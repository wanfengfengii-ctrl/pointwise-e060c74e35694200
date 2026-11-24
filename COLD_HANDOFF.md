# 原批冷档交接

白班照常用 README.md 的入口把采样报送签入日账。库管拿一张正式入仓回执，另办一张原批冷藏单。源仓与冷库使用固定的一对 SQLite 文件，冷库独立于源仓和 V1 目录。两端只能经调用方提供的消息通道交换材料。通道可信，可能扣住请求、重投消息或丢掉处理完的回复。没有回复时可以重新调用。成功冷藏持续占用唯一冷槽。本次只办完整原批，不办理扩槽、迁卷和已确认原件退档。

## 源仓入口

在 `publication.cold_handoff` 提供 `initialize_source(warehouse)` 与 `TransferDesk(warehouse, exchange)`。`warehouse` 是已按 V1 初始化的源仓连接。初始化可重跑，调用方负责关库。交接号及源回执号都是非空文本。

| 调用 | 行为 |
| --- | --- |
| `desk.open_transfer(key, receipt_id)` | 开办并固定已签发的确切原批。同号同参返回已有状态。同号改绑抛 `ValueError`。首次开办以 V1 原件验领为准，未知或不可验的原件不得开办。 |
| `desk.advance(key)` | 办理或续办。槽被其他交接占用时可返回 `full`，原单以后仍可续办。无故障且槽可用时可办成。 |
| `desk.cancel(key)` | 请求撤销并返回可认定的交接结果。确认已胜时须返回原冷藏首执。取消已胜并且旧操作已不能影响新占用后，才可返回 `cancelled`。 |
| `desk.status(key)` | 查办，可向冷库补认已落定结果，不得把未裁决的申请自行办成。通道正常时可以查回已落定但曾失答的结果。 |

结果对象公开 `state` 和 `receipt`。`state` 取 `pending`、`full`、`cancelled` 或 `confirmed`。只有 `confirmed` 的 `receipt` 是非空 `bytes`，其余为 `None`。首执按字节复领，已认定的终态不倒退。对未知交接调用续办、撤销或查办抛 `KeyError`。交换抛 `TimeoutError` 且本次仍不能认定结果时可返回 `pending`。数据库错误仍可由调用方识别，失败的冷库提交不会交出新首执。

## 冷库入口及投递

在 `archive.cold_storage` 提供 `initialize_cold(cold)`、`ColdSlot(cold)` 及 `cold_claim(cold, receipt)`。`cold` 可用 `publication.storage.open_warehouse(cold_path)` 打开，使用独立的库文件和冷库初始化。该初始化可重跑。源仓调用 `exchange(phase, message)`，回送 `bytes`。测试通道可把请求送给 `ColdSlot(cold).receive(phase, message)`。`phase` 是字符串，`message` 是不可变字节。两端自行约定消息正文。

为联调区分两个停点。用 `write` 标识实写完整候件的消息，这次成功处理后冷库已耐久存有候件，但尚不能凭首执领件。用 `confirm` 标识在冷库建立首执领取资格的消息。用 `cancel` 标识传给冷库的撤销或撤销处置消息。可使用其他辅助消息。夹具只按这些标识控制投递。先实写整件，再到确认停点。确认或撤销以协议实际落定的唯一决定为准，不能从回包到场次序判断。

冷库最多保有一份用于交接或领取的原批正文。撤销胜出后，应可复用该槽，原交接不能再取回占用或确认资格。旧写、旧确认和旧清槽无权改动后继占用、其正文或首执。确认胜出后原件和首执保持可领，重投或撤销不能释放该槽。`cold_claim` 只凭冷库与首执核对所承诺的交接、源回执、原批身份和整批完整性，返回 V1 的 `archive.retrieve.ClaimedBatch`。按实际原序保留采样口、业务日、设备、原行号、原值、单位及缺测。错执和坏件抛 `ValueError`，不得返回部分行。

## 样例与交付核验

`python3 -B cold_fixture.py` 从空 V1 库签出两批样例并核对源账。夹具的 `seed(catalog, warehouse)` 返回以 `A`、`B` 为键的源回执号。`Mailbox(handler)` 可用作 `exchange`。调用 `hold_next(phase)` 扣住下一条匹配请求，调用 `lose_next_reply(phase)` 则先处理该请求再丢回复，均以 `ReplyLost` 表示未收到回复。`sent` 记录发出的 `(phase, message)`，`held` 记录扣住的请求，`lost_replies` 记录已处理的请求及被丢掉的回复。重开冷库时可替换 `handler`。用 `deliver(packet)` 递交捕获的原消息。若释放是撤销处理的一部分，重投撤销消息即可核验旧清槽。若选定协议在撤销胜序下无法产生有效旧确认，则核验续办不能再产生有效确认。

- A 为设备 `device-p` 第 17 行 7 mL，B 为同设备第 4 行 11 mL。让 A 实写后失答并撤销，再确认 B。迟到或重投可发出的 A 旧写、旧确认或旧清槽后，A 仍是 `cancelled`。关闭源仓与目录，重开冷库，凭 B 首执取得源回执 B 对应的第 4 行原批。
- 确认 A 已在冷库形成领取资格，但丢掉回复。两库重开后，查办、同号续办或撤销应认回同一个首执。B 仍报槽满。改绑和错执须拒绝。
- 模拟冷库确认提交失败。可用夹具的 `reject_next_commit(cold)` 注入一次提交拒绝。该次无新首执，故障解除后原交接可续办。另用 `sample.json` 的混合单位和缺测原批验领原序，再损坏冷库正文并核对拒领。两种胜序下，V1 仍可核对两批原件、原回执和 18 mL 日账。

沿 `.pointwise-runtime.json` 的 `verify` 保留 `check_project.py` 原回归并核验冷档交接，业务检查失败须以非零状态退出。沿 `demo` 保留原演示并展示撤销后复用同槽、重开领取 B 原批及原日账。运行清单沿用现有格式与入口，原测试字节照常保留。

# 古籍数字借展管控

服务用于协调海外馆藏古籍（英、法、日、德等机构）的数字母版、展示许可与发布窗口，
确保每次公开使用都能核对来源、许可与署名。

- `python3 service.py --check` 核对服务配置
- `python3 service.py --port 8000` 启动，`GET /health` 返回服务身份
- `npm test` 运行全部契约测试（Python unittest：基础契约 + 领域规则 + HTTP 端到端）

## 模块

| 文件 | 职责 |
| --- | --- |
| `domain.py` | 领域核心（无 Web 依赖）：交付核验、版本谱系、许可窗口、批准快照、令牌/缓存失效、台账、馆藏方隔离、导出防泄漏 |
| `service.py` | JSON HTTP 接口与健康检查 |
| `test_domain.py` | 20 个领域契约测试（含可注入时钟验证时区边界） |
| `test_http_api.py` | 端到端 HTTP 流程测试 |
| `service_contract.py` | 原有健康检查契约 |

## 合规要求如何落地

- **交付与校验值**：馆藏方交付时按申报 SHA-256 核验，不符即拒收且不留存任何记录
  （`POST /api/masters/deliver`）。母版字节只在内存中参与计算，系统不保存文件内容，
  任何接口、日志（访问日志已关闭）、馆藏方报告、导出包都不含母版字节或直链。
- **书目版本/页序/缺损/色彩/衍生图关联**：`Edition` 持有页序、缺损说明、色彩校准，
  每张衍生图登记其来源母版与自身校验值；禁止把高清母版本身登记为衍生图。
- **发布前三重核对**：批准（`POST /api/editions/{id}/approvals`）同时要求
  图像许可有效、文字解说许可有效、署名（credit line）非空，且地域被两项许可覆盖、
  母版不在开幕前封闭期内。批准时固化完整快照（母版/衍生图校验值、许可 id、页序、色彩）。
- **替换扫描件**：旧母版保留谱系（`replaced_by`），旧衍生图关联失效，必须重做并重新批准；
  决策纪元（epoch）+1，旧令牌与形如 `v1:<edition>:<derivative>:<territory>` 的缓存键作废。
- **缩短授权 / 临时下架**：许可从不就地改写——缩短产生更短的新许可、旧许可标记
  `superseded_by`；下架吊销全部现行许可与批准。两者均推进纪元，已签发的下载/展示链接立即失效。
- **修正页序**：旧版本冻结为新版本（`superseded_by` 指向 v2），只允许重排不允许增删页；
  旧批准与链接失效，历史快照可经 `/restore`（可带 `approval_id`）复原。
- **时区**：许可窗口按馆藏方当地时间（`Asia/Tokyo`/`Europe/London`/…）授予，
  授予时换算成绝对 UTC 存储并贯穿批准、令牌签发与访问校验，夏令时切换也按绝对时间判定，
  不会提前开窗或逾期仍可访问。
- **公开溯源**：`GET /api/public/editions/{id}/provenance?territory=CN`
  对任一公开页面说明所用母版（id + 校验值）、衍生图、批准 id 与双重许可 id、署名要求。
- **馆藏方隔离**：馆藏方账号只在自己机构范围内交付、替换、缩短、下架，
  `GET /api/lender/report` 只返回本馆资料的访问与使用记录；跨馆访问统一返回 404，
  不透露他馆书目是否存在。
- **导出包**：`POST /api/editions/{id}/export` 只输出当前窗口内经批准的低清衍生图元数据
  与下载引用，明确 `contains_master: false`；纪元变化后包内链接失效，须重新生成。

## 接口一览

```
POST /api/institutions                     登记合作馆（含时区）
POST /api/users                            策展人 / 馆藏方账号
POST /api/editions                         建书目版本（页序/缺损/色彩校准）
POST /api/editions/{id}/correct-page-order 修正页序（旧版冻结，返回新版本）
POST /api/masters/deliver                  交付母版 + 校验值（不符拒收）
POST /api/masters/{id}/replace             替换扫描件
POST /api/masters/{id}/derivatives         登记衍生图（关联母版）
POST /api/editions/{id}/texts              登记文字解说
POST /api/editions/{id}/licenses           授予图像/文字许可（当地时间窗口+地域）
POST /api/licenses/{id}/shorten            缩短授权
POST /api/editions/{id}/takedown           临时下架
POST /api/editions/{id}/approvals          发布前三重核对与批准
POST /api/approvals/{id}/tokens            签发展示/下载链接（含缓存键与到期时间）
GET  /api/tokens/serve/{token}?territory=  公众访问（窗口/纪元/地域实时校验）
GET  /api/public/editions/{id}/provenance  公开溯源
GET  /api/lender/report?actor=             馆藏方使用记录（仅本馆）
POST /api/editions/{id}/restore            复原已展示版本的批准快照
POST /api/editions/{id}/export             生成导出清单（不含高清母版）
GET  /api/ledger                           决策台账（馆藏方仅见本馆事件）
GET  /health                               服务身份
```

> 说明：交付文件以 base64 放在 `payload_b64` 字段；服务端不持久化文件字节。
> 生产部署应把母版字节留在由硬件密钥管控的对象存储中，由本服务的校验值与
> 许可状态门控访问，而不是让字节经过应用层。

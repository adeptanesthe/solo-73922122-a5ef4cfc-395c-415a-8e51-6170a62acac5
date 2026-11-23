# 合唱排练节拍提示轨服务

给合唱团排练录音配一条**可叠放**的节拍提示轨（click track）。接收 16 kHz / 16 位 / 单声道 PCM WAV（≤ 30 秒）与每 10 ms 一帧的起音分数，在人工确认的拍点之间求解**最高分节拍线**，只给拍点完全确定的小节加提示音，第一拍为重音。

## 规则

- 每小节 4 拍；可指定 1～8 小节（4～32 拍）。
- 拍间距（相邻拍帧号之差）必须在请求的 `min_gap_frames`～`max_gap_frames` 内，且该范围本身限制在 20～100 帧。
- 相邻两段拍间距最多相差 2 帧（`|gap[i+1] - gap[i]| ≤ 2`）。
- 第一拍与最后一拍必须人工确认，且节拍线必须精确经过所有已确认拍点。
- 总分为各拍起音分数之和，取总分最高的节拍线。
- 某小节只有在**所有**最高分节拍线中四拍位置都一致时，才整小节加提示音；否则整小节留白，并在响应的 `ambiguous_measures` 中返回其小节号。
- 提示轨与原录音**等长、同规格**（16 kHz / 16 位 / 单声道，采样数完全一致）。
- 重新提交人工拍点后：若不存在合法节拍线，返回 422 并**保留当前版本**；若求解成功则生成新版本，旧版本下载返回 409。

## 用 Docker Compose 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000，交互文档 http://localhost:8000/docs
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `POST` | `/api/recordings` | 上传录音 + 标注，创建第 1 版提示轨 |
| `POST` | `/api/recordings/{id}/beats` | 重新提交人工拍点，成功则换版 |
| `GET` | `/api/recordings/{id}` | 录音元数据、当前版本与版本历史 |
| `GET` | `/api/recordings/{id}/versions/{n}/cue.wav` | 按版号下载提示轨（旧版 409） |

### 上传

`multipart/form-data`：

- `audio`：WAV 文件（16 kHz、16 位、单声道、PCM，≤ 30 秒）
- `annotation`：JSON 字符串

```json
{
  "measure_count": 2,
  "min_gap_frames": 38,
  "max_gap_frames": 46,
  "confirmed_beats": [
    {"beat": 1, "frame": 10},
    {"beat": 8, "frame": 290}
  ],
  "onset_scores": [0, 0, "...每 10 ms 一帧，长度 = ceil(采样数/160)，取值 0..1000"]
}
```

`beat` 为 1 基拍号（第 1 小节第一拍为 1），`frame` 为 0 基帧号。`onset_scores` 的长度必须等于 `ceil(采样数 / 160)`。

### 响应要点

```json
{
  "recording_id": "9b7c…",
  "audio": {"sample_rate": 16000, "channels": 1, "bit_depth": 16,
            "sample_count": 48000, "duration_seconds": 3.0, "onset_frames": 300},
  "current_version_number": 1,
  "current_version": {
    "version": 1,
    "is_current": true,
    "total_score": 800,
    "beat_positions": [10, 50, 90, 130, null, 210, 250, 290],
    "ambiguous_measures": [2],
    "cued_measures": [1],
    "download_url": "/api/recordings/9b7c…/versions/1/cue.wav"
  },
  "versions": [{"version": 1, "is_current": true, "...": "..."}]
}
```

`beat_positions` 中 `null` 表示该拍在最高分节拍线中位置不唯一。

### 重提拍点

```json
{"confirmed_beats": [{"beat": 1, "frame": 10}, {"beat": 5, "frame": 170}, {"beat": 8, "frame": 290}]}
```

成功：`201`，返回新版本，`is_current: true`，版本号递增。
失败（无合法节拍线）：`422`，`detail.code = "no_legal_beat_grid"`，并附带 `current_version`，原版本与其下载保持不变。

下载已作废的旧版本：`409`，`detail.code = "version_superseded"`。

### 错误码

| HTTP | `detail.code` | 场景 |
| --- | --- | --- |
| 422 | `invalid_field` / `invalid_annotation` | JSON 非法、参数越界、确认拍点顺序错误 |
| 422 | `invalid_audio` | 非 PCM WAV、非 16 kHz / 16 位 / 单声道、超过 30 秒、文件损坏 |
| 422 | `frame_count_mismatch` | `onset_scores` 长度与音频帧数不符 |
| 422 | `missing_endpoint_marks` | 首拍或末拍未确认 |
| 422 | `no_legal_beat_grid` | 确认拍点下无满足间距/±2 帧约束的节拍线 |
| 404 | `recording_not_found` / `version_not_found` | 录音或版本不存在 |
| 409 | `version_superseded` | 下载已作废的旧版本 |
| 413 | `file_too_large` | 上传超过 4 MiB |

## 演示请求

```bash
docker compose up --build -d
python3 scripts/make_demo_inputs.py /tmp/demo

curl -s -X POST http://localhost:8000/api/recordings \
  -F "audio=@/tmp/demo/take.wav;type=audio/wav" \
  -F "annotation=$(cat /tmp/demo/annotation.json)" | tee /tmp/created.json

RID=$(python3 -c 'import json;print(json.load(open("/tmp/created.json"))["recording_id"])')

# 下载第 1 版（第 5 拍歧义 → 第 2 小节留白）
curl -OJ http://localhost:8000/api/recordings/$RID/versions/1/cue.wav

# 重提拍点：确认第 5 拍 → 生成第 2 版，两小节都有提示音
curl -s -X POST http://localhost:8000/api/recordings/$RID/beats \
  -H 'Content-Type: application/json' \
  --data @/tmp/demo/reannotation.json | python3 -m json.tool

# 旧版本下载 → 409
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/api/recordings/$RID/versions/1/cue.wav
```

完整的逐步演示（含长度/格式校验与失败重提）：`./scripts/run_demo.sh`。

## 本地测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

求解器另有一组与**穷举法逐一比对**的随机测试（`tests/test_solver.py`），验证候选拍点集合与总分完全一致。

## 结构

```
app/
  main.py     FastAPI 路由、校验、错误映射
  models.py   请求模型与参数约束
  solver.py   精确前向/后向动态规划 + 最优线候选集
  cue.py      提示音合成（首拍重音）
  wav_io.py   严格 WAV 读写与格式校验
  store.py    线程安全的内存版本存储
tests/        求解器、音频、API 端到端测试
scripts/      演示输入生成与演示脚本
```

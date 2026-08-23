# ASR 调优总文档

## 文档目的

本文档记录 CapsWriter for macOS 当前阶段的 ASR 调优总口径、第一轮评测数据集组合方案，以及首要需要解决的问题。

当前调优目标不是泛化评测所有 ASR 能力，而是围绕 CapsWriter 的真实产品场景建立可重复的评测闭环：

- 中英文语音输入。
- 技术口述、开发者术语、会议式表达。
- 便携电脑端小声说话、低语、收音不理想的使用场景。
- 本机 16G 内存可承受的短周期验证。

## 已收敛口径

### 不自录数据集

第一轮不要求用户自录数据集。数据来源优先使用公开数据集、固定抽样和极少量可复现派生样本。

原因：

- 用户当前没有稳定、系统化收集自录数据的条件。
- 评测集需要先可复现，避免每轮调优的样本来源变化。
- 公开数据足够支撑第一轮基线判断。

### 不改当前产品预处理链路

第一轮调优不新增响度归一化、AGC、降噪、EQ、混响补偿等产品预处理。

当前管线事实：

- 客户端录音拿到 `sounddevice.InputStream(dtype="float32")` 原始输入。
- 发送服务端前做 48kHz 到 16kHz 的简单抽样和通道平均。
- 服务端把 `bytes` 转回 `float32` 后直接交给 ASR 引擎。
- `qwen_asr_mlx` 适配层只做 `float32` 化和必要重采样。
- `mlx-qwen3-asr` 的音频加载只做单声道、采样率、PCM 范围转换和 Whisper 风格 log-mel 特征，不做 RMS/LUFS/AGC 级响度归一化。

因此第一轮评测目标是在现有管线下观察模型和接入策略的真实表现，而不是先改变输入处理。

### 评测必须走 CapsWriter 推理后端

评测驱动不能直接调用 `mlx_qwen3_asr.transcribe()` 或子仓库自带 benchmark 入口作为主结果来源。

原因：

- 真实产品路径是客户端为一次录音或一次文件转写生成一个 `task_id`，并持续发送带有同一 `task_id` 的 `AudioMessage`。
- 当前旧服务端路径会把同一个 `task_id` 下的连续音频切成多个代码层 `Work` 对象，再由 `WorkPipeline` 调用 `QwenASRMLXEngine`，最后进入 `mlx-qwen3-asr`。
- 如果评测绕过 `QwenASRMLXEngine`，就会漏掉 CapsWriter 当前实际使用的语言映射、context 透传、音频采样率整理、结果格式化和性能元数据。
- 调优目标是改善 CapsWriter 的实际输入体验，不是单独评估上游库裸 API。

第一轮评测驱动应优先做到：

- 在 runner 重构落地前，读取 manifest 音频后可构造与旧服务端一致的 `Work` 作为临时基线。
- 在 Qwen3-ASR runner 落地后，评测主路径必须以同一个 `task_id` 调用 package runner，而不是绕回旧服务端 `Work` 分片。
- 保存 raw ASR 输出、最终格式化输出、耗时、RTF、`finish_reason`、`truncated`、语言配置、context 配置和模型路径。

首轮不强制走 GUI、WebSocket 或真实麦克风录音链路。这样可以避免把客户端权限、网络连接、前台窗口和快捷键问题混入 ASR 质量评估。

### MLX 后端配置归属

当前 `mlx-qwen3-asr` 已作为本项目根目录子仓库接入，`requirements-server.txt` 在 macOS 上从本地子仓库安装该包。后续推理调优应基于这个可编辑源码路线继续推进，而不是继续把上游包当作完全黑盒。

配置归属原则：

- CapsWriter 外层只保留产品级选择：启用哪个 ASR 后端、模型目录解析、客户端传入的 `language` 和 `context`。
- `mlx-qwen3-asr` 包内承载推理级配置：prompt 组装、语言强制策略、generation config、`max_new_tokens` 自适应策略、预热、MLX wired memory、chunking、aligner 接法等。
- 不在 CapsWriter 外层和 `mlx-qwen3-asr` 包内重复维护同一类推理参数。
- 如果某个参数只是为了调优 `mlx-qwen3-asr` 的推理行为，应优先在子仓库内形成集中配置，再由 CapsWriter 适配层传入一个明确配置对象或保持默认。
- 权重常驻、启动预热这类运行配置可以由 server 作为开关或资源预算透传给 editable package，但实现必须在 package 内部完成，由 package 调用 `mlx.core` 等底层接口；server 不直接调用 MLX 底层 API，也不复制 package 内部策略。

因此，在正式评测大矩阵之前，必须先跑通“CapsWriter 使用本地子仓库源码包”的后端路径，并完成第一轮配置归属收敛。否则评测结果可能对应的是旧黑盒接法，后续迁移源码后又需要重跑。

进一步明确：

- 所有会影响 ASR 推理输出的参数，统一集中在 editable `mlx-qwen3-asr` package 内。
- CapsWriter server 端调用实际推理引擎时，不再负责控制模型推理参数。
- server 端可以传入音频和请求元信息，例如来源、语言选择、用户 context、task id；但不应在 server 外层决定 generation、prompt、chunking、aligner、预热、wired memory 等推理策略。
- 对权重常驻等非文本输出参数，server 只负责传入“启用/禁用、预算上限”等运行意图；具体如何计算 wired limit、何时预热、如何调用 MLX，归 editable package 的 runner 或中层编排层所有。
- 如果某个现有 server/client 配置会改变识别文本结果，例如长录音分段长度、overlap、拼接策略，它也应被视为 ASR 管线策略，后续需要迁入 package 或由 package 暴露统一实现，供 server 与评测 driver 共同调用。

### 当前音频与长录音层级

当前产品链路分为几层。

1. 客户端采集与传输

- 麦克风路径：客户端从 `sounddevice` 获取设备原始 `float32` 音频，当前实际按 48kHz 输入处理。
- 发送服务端前，客户端用 `data[::3]` 简单抽样到 16kHz，并对多声道取均值，形成 16kHz、mono、float32 bytes。
- 文件路径：客户端通过 FFmpeg 输出 16kHz、mono、float32 PCM，再按字节流发给服务端。
- 这一层属于产品输入和传输，不属于模型推理参数；但它会影响音频质量，评测时必须复现或明确绕过。

2. 服务端 WebSocket 接收与产品级分段

- `AudioMessage` 携带 `seg_duration` 和 `seg_overlap`，当前默认麦克风和文件都是 60 秒分段、4 秒重叠。
- 服务端 `ws_recv` 按 `seg_duration + seg_overlap * 2` 作为提交阈值，达到阈值后提交一个 `seg_duration + seg_overlap` 长度的 `Work`，再按 `seg_duration` 前进。
- 因此当前典型片段是 64 秒音频，步长 60 秒，片段之间有 4 秒重叠。
- 录音或文件结束时，服务端把剩余缓存作为最终片段提交。

3. 服务端任务处理与结果拼接

- `process_audio_work()` 只把当前代码中的 `Work.data` 从 bytes 转成 `np.float32`，并统计时长；不做 AGC、响度归一化、降噪或 EQ。
- `WorkPipeline` 为每个片段创建 `RecognitionStream`，调用 `QwenASRMLXEngine.decode_stream()`。
- 每个片段的 raw 文本由 `merge_by_text()` 跨片段拼接。
- 如果有 token/timestamp，则用 `merge_tokens_by_sequence_matcher()` 做时间戳路径拼接。
- 最终阶段再走 `TextFormatter` 格式化。

4. `mlx-qwen3-asr` package 内部推理分段

- package 可以直接接收 `np.ndarray` 或 `(np.ndarray, sample_rate)`，会转换为 16kHz mono float32。
- package 内部还有自己的长音频分段：`split_audio_into_chunks()` 默认把超过 30 秒的音频按低能量点递归切成更短 chunk。
- 因此当前 CapsWriter 的一个 64 秒服务端片段，进入 package 后还会被 package 再切成约 30 秒级别的内部 chunk。
- package 内部会把这些内部 chunk 的文本合并为单个片段结果，然后交回 CapsWriter 的 `WorkPipeline` 做产品级跨片段拼接。

### `task_id` 与 `Work` 语义收敛

当前代码里的命名存在一处历史混用，需要在 Qwen3-ASR 重构前先明确。

已经确认的事实：

- `AudioMessage.task_id` 是客户端一次按键录音或一次文件转写生成的稳定 ID。
- 同一个完整音频任务在客户端到服务端的传输过程中，会产生多个 `AudioMessage`，但它们共享同一个 `task_id`。
- `WorkerState.sessions` 以 `task_id` 为键保存 `RecognitionSession`，说明 worker 侧也把 `task_id` 当作完整识别会话标识。
- 当前代码中的 `Work` 类不是完整任务，而是旧服务端按 60 秒分段、4 秒 overlap 切出来的 worker 执行单元。
- 因此当前旧链路实际结构是：一个 `task_id` / 一个完整识别任务 / 一个 `RecognitionSession`，下面可以有多个代码层 `Work` 分片。

后续统一口径：

- `task_id` 就是完整识别任务标识，也等价于当前讨论中的 record session / recognition session 标识。
- 不再额外引入 `RecordSession` 作为新的业务层级，避免把同一层含义拆成两个名字。
- 旧后端仍可在一个 `task_id` 下产生多个 `Work`，以保留现有 60 秒分段、4 秒 overlap、跨片段拼接能力。
- Qwen3-ASR 新路径不再使用旧 `Work` 表达 ASR 语义切片；一个 `task_id` 对应一个 package runner 生命周期。
- Qwen3-ASR runner 内部约 30 秒级别、真正送入模型的单位命名为 `InferenceChunk`，由 runner 自己负责切分、推理和拼接。

命名层级固定如下：

- `AudioMessage`：客户端到服务端的 WebSocket 协议消息，包含 `task_id`、音频数据、`is_final`、语言和 context 等字段。
- `task_id`：一次完整录音或一次文件转写的唯一标识；它就是完整识别任务标识，也就是 record session / recognition session 标识。
- `Work` / `RecognitionWork`：旧后端的 worker 执行单元；当前主代码已经改用 `Work` 命名，避免和 `task_id` 混淆。
- `AudioFeedPatch`：Qwen3-ASR 新路径中，server/worker 按时间顺序喂给 `QwenASRMLXEngine` / package runner 的内部音频增量。它不是 WebSocket 包，也不是推理 chunk；它只表达同一个 `task_id` 下新增的一小段连续音频、时间顺序信息、offset 或 sample 游标、以及 final 标记。
- `InferenceChunk`：package runner 内部拼到稳定边界后，真正送入 Qwen3-ASR 模型推理的约 30 秒级单位。
- `QwenASRRunner`：editable package 内 CapsWriter 专用 runner，负责按 `task_id` 管理音频缓冲、`AudioFeedPatch` 拼接、`InferenceChunk` 切分、推理、结果拼接、`finish_reason` / `truncated` 等元数据收集，并在 final 后返回该 `task_id` 对应的完整结果。实现时优先复用 package 现有的 `split_audio_into_chunks()`、内部 transcribe 编排和结果拼接逻辑，不在 server 侧重写这些策略。

推荐结构：

```text
AudioMessage(task_id, data, is_final, ...)
  └─ task_id = 完整识别任务 / recognition session
       ├─ 旧后端：多个 Work
       └─ Qwen3-ASR：多个 AudioFeedPatch（按时序 feed）
            └─ QwenASRRunner
                 └─ 多个 InferenceChunk
```

这个方案是当前旧代码现状下的最小重构路径：保留跨 client/server/worker 已经存在的 `task_id` 协议字段，只修正服务端内部执行单元命名和 Qwen3-ASR 的切分归属。

### 评测 driver 与产品链路对齐原则

评测 driver 不能直接调用低层 `Session.transcribe()` 来代表产品结果。

原因：

- 低层调用会绕过 CapsWriter 当前 60 秒分段、4 秒 overlap、跨片段文本拼接、最终格式化和服务端任务状态。
- 低层调用会只使用 package 内部 30 秒 energy chunking，这与真实产品长录音路径不一致。
- 如果 driver 把整段长音频直接交给 package，结果可能优于或劣于产品实际结果，但无法解释到真实用户体验。

合理目标应改为：

- editable package 内提供 CapsWriter 专用推理实例或 runner，集中持有所有推理参数和推理策略。
- 当 server 配置 `qwen_asr_mlx` 后端时，链路在服务端识别调度处按后端分叉：不再走旧 `Work` 切分和旧 `WorkPipeline` 跨片段拼接，而是把同一个 `task_id` 下的 `AudioFeedPatch` 按时序 feed 给 package runner。
- CapsWriter server 调用这个 package runner，不在外层改写推理参数，也不负责推理级切分和结果拼接。
- 评测 driver 也调用同一个 package runner。
- 对于短音频评测，driver 可以直接把 16kHz mono float32 或 `(audio, sample_rate)` 传给 runner。
- 对于长录音评测，质量评测可以一次性传入完整音频；时延评测需要模拟 server 向 runner 持续 feed 音频，但切分和拼接仍由同一个 runner 负责。

因此，“driver 直接运行 editable package 推理实例”是合理的，但前提是这个实例不是裸 `Session.transcribe()`，而是 CapsWriter 和评测共同使用的同一层 package runner。若只是裸调 package 低层 API，则会破坏评测与实际使用链路的一致性。

### 不做 Qwen3-ASR 流式推理

当前 ASR 调优只针对 Qwen3-ASR / `qwen_asr_mlx` 这条模型路线，不把 streaming 作为目标。

明确约定：

- CapsWriter 专用 package runner 不实现产品级 streaming 文本输出。
- `mlx-qwen3-asr` 上游 package 虽然提供了 `init_streaming`、`feed_audio`、`finalize_streaming` 等伪流式能力，本轮明确不用这条路径。
- runner 以 `task_id` 为完整任务生命周期，可以接收 server 持续喂入的音频数据；这只是内部音频 feed，不是产品级流式识别输出。
- 语义切段、overlap、prompt、language、generation、结果拼接、`finish_reason`、`truncated` 等推理管线逻辑，都从 runner 开始并由 runner 统一拥有。
- Server 可以继续保留现有客户端到服务端的传输分包、WebSocket、队列、buffer 等机制，但这些只属于 I/O 和传输层。
- Server 不应再把每个传输包当作 ASR 语义片段调用模型。
- Server 现有历史上的流式、实时回显或分片处理代码，本阶段不主动改动；如果未来要做 Server 流式体验，那是独立课题，不纳入本轮 Qwen3-ASR 推理调优。

第零阶段不采用上游伪流式，但也不能简单牺牲长录音的提前计算能力。

需要区分两种实现形态：

1. 简单完整录音后推理。
   - Server 收完一次录音后，把完整音频交给 runner。
   - 优点是实现最简单，评测最容易对齐。
   - 缺点是超过约 68 秒的长录音会失去旧链路“录音未结束就开始推理”的能力，长录音最终结果时延代价偏大。

2. package-owned 流式喂音频 + 离线最终结果。
   - Server 仍然按时间顺序把音频增量送入 worker/package；进入 Qwen3-ASR runner 前的内部增量统一命名为 `AudioFeedPatch`。
   - `AudioFeedPatch` 只作为 I/O 增量数据，不作为 ASR 语义片段，不触发旧 `WorkPipeline` 的跨片段拼接逻辑。
   - Server 和 runner 之间可以是流式音频传输；这只是“喂音频”的流式，不是 ASR streaming 输出。
   - runner 维护同一次录音任务的音频缓冲和内部处理游标。
   - runner 自己决定哪些内部 chunk 已经稳定、可以提前推理。例如录音达到约 30 秒后，runner 就可以开始处理第一段稳定 chunk，而不必等到 Server 原来的 68 秒阈值。
   - runner 不输出实时 partial，不接上游 `init_streaming/feed_audio/finalize_streaming` 伪流式路径。
   - final 到达后，runner 完成剩余音频推理和最终拼接，只返回完整结果。

推荐方向是第二种：package-owned 流式喂音频 + 离线最终结果。

这样可以同时满足：

- Server 不再拥有 ASR 语义切段和拼接策略。
- 语义 chunking、overlap、prompt、language、generation、拼接仍统一在 package runner 内。
- 长录音可以保留“录音过程中提前处理已稳定音频”的性能优势。
- 评测 driver 可以调用同一个 runner；质量评测可直接传完整音频，时延评测可模拟分包 feed。

实现边界补充：

- 其它 ASR 后端继续保留当前 Server 架构：客户端持续发音频，Server 按 60 秒分段、4 秒 overlap 提交多个 `Work` 执行单元，再由 `WorkPipeline` 拼接。
- `qwen_asr_mlx` 单独新增代码路径：Server 不再把中间传输包转换成多个 ASR 语义片段，也不再通过旧 `WorkPipeline` 进行片段推理和拼接；中间音频增量转换为 `AudioFeedPatch`，持续喂给 package runner 的同一个 `task_id` 生命周期。
- 对 `qwen_asr_mlx` 来说，客户端一次按键录音或一次文件转录就是一个完整 ASR 任务；这个完整任务可由多个传输分包组成，但只有 runner 可以决定推理级切段和提前计算时机。
- 这条路径下，音频进入 `QwenASRMLXEngine` 后交给 package runner；正式推理、推理级切段、overlap、prompt、language、generation 和拼接全部由 runner 管理。
- 这不是删除旧 Server 分片机制，而是按后端分叉：保留旧模型所需的 Server 分段，同时让 Qwen3-ASR 路线实现“完整任务进入 runner”的新口径。

### 现有非推理优化与速度影响

CapsWriter 原有链路在推理前已经做了一些非模型层优化。

客户端侧：

- 麦克风输入流按 50ms block 回调，只在录音状态下把音频放入异步队列。
- 录音超过快捷键触发阈值后，客户端边录边把音频通过 WebSocket 发给 Server。
- 发送前客户端把 48kHz 输入用 `data[::3]` 简单抽样到 16kHz，并把多声道平均成 mono。
- WebSocket 传输的是 16kHz mono float32 bytes 的 base64 编码。

Server 侧：

- `ws_recv` 边接收边缓存音频 bytes。
- 对现有后端，Server 默认按 60 秒分段、4 秒 overlap 形成代码层 `Work`。
- 实际提交阈值是 `seg_duration + seg_overlap * 2`，默认约 68 秒；提交片段长度是 `seg_duration + seg_overlap`，默认约 64 秒；步长 60 秒。
- 因此长录音或长文件可以在接收过程中逐段进入 worker 推理。
- `WorkPipeline` 负责跨片段文本拼接、可选 token/timestamp 拼接和最终格式化。

速度影响判断：

- 对普通短按录音，影响很小。原因是短录音通常不到 68 秒，旧链路本来也不会在 final 前提交中间 ASR 片段；改成 Qwen3-ASR 单个 `task_id` runner 生命周期后，仍然是松手后完成最终结果。
- 对超过约 68 秒的长麦克风录音，如果采用“完整录音结束后再推理”的简单实现，Qwen3-ASR 会失去旧 Server 的“边录边提交 ASR 片段”能力，最终结果时延代价偏大。
- 对长文件转录，如果采用简单完整任务后推理，也会失去“边传文件边解码”的流水线重叠，总体 wall-clock 可能变长。
- 因此后续实现应优先考虑 package-owned 流式喂音频 + 离线最终结果：不输出流式文本，但允许 runner 在录音/传输过程中提前处理已经稳定的内部 chunk。
- 但新路径会减少外层 60 秒分段 + overlap + 跨片段拼接带来的重复推理和拼接误差，长音频质量归因会更清晰。
- Qwen3-ASR package 内部仍会做自己的推理级 chunking，因此完整录音进入 runner 不等于模型一次性吃完整长音频。

本轮取舍：

- 当前 Qwen3-ASR 调优优先保证推理链路一致性、评测可复现和参数 owner 集中。
- 不把产品级流式文本输出作为本轮目标。
- Qwen3-ASR 长录音的提前计算应在 package runner 层实现：server 只持续 feed 同一个 `task_id` 的音频，runner 自己决定内部 `InferenceChunk` 的稳定边界和处理时机。
- 若后续确实需要 Qwen3-ASR 实时 partial 体验，应作为独立课题重新设计，而不是复用上游伪流式或恢复 Server 语义分段。

### 子仓库代码评估

`mlx-qwen3-asr` 的代码整体可以作为后续调优基础，但当前 CapsWriter 接入方式还停留在最小适配层。

已确认的优点：

- 上游库有明确的 `Session` API，模型和 tokenizer 生命周期集中在一个对象里，适合服务端常驻进程。
- 推理入口参数集中在 `Session.transcribe()` / `transcribe()` 一层，包含 `context`、`language`、`return_timestamps`、`max_new_tokens`、`draft_model`、`num_draft_tokens`、`diarize`、`forced_aligner`、`return_chunks`、`verbose`。
- 生成配置集中在 `GenerationConfig`，当前默认是 `temperature=0.0` 的确定性解码，并有 `finish_reason`、`truncated` 等可观测字段。
- `max_new_tokens` 有按音频时长自适应的默认策略，可以先作为基线观察，不必第一轮手写 token 上限。
- 子仓库内已有 benchmark、manifest、质量门禁等材料，适合借鉴指标和报告形态。

当前接入的不足：

- CapsWriter 的 `QwenASRMLXEngine` 只暴露 `model`、`return_timestamps`、`max_new_tokens`、`verbose`，还没有接管上游中层推理编排。
- `context` 和 `language` 虽然已从客户端任务透传，但策略仍是“直接传入”，没有 CapsWriter 场景化的 prompt/context 构造层。
- 服务端当前 `.venv` 中导入的包路径是 `site-packages/mlx_qwen3_asr` 安装副本，不是直接指向根目录子仓库源码；因此修改子仓库源码后是否立刻生效还需要先通过 editable install 或等价机制收敛。
- 上游自带 benchmark 主要评估 `mlx-qwen3-asr` 裸库能力，不能直接代表 CapsWriter 实际管线质量。

第一轮结论：

- 子仓库代码质量足够继续投入，不需要另起炉灶。
- 但第一轮正式评测前，必须先把服务端实际加载路径改成可验证的本地源码路径。
- 后续调优应在 `mlx-qwen3-asr` 包内形成 CapsWriter 专用中层编排，而不是继续把所有策略堆在 `core/server/engines/qwen_asr_mlx/asr_engine.py` 适配层。

### 第一轮调优范围

第一轮只纳入最有解释力、最可能影响 CapsWriter 体验的少数调优点。

必须纳入：

- 模型规格：`1.7B-8bit` 与 `1.7B-4bit` 的质量和耗时对比。
- 语言策略：`auto` 与按样本强制 `Chinese` / `English` 的差异。
- context 策略：无 context 与技术场景 context 的差异。
- token 预算观测：记录 `max_new_tokens` 实际配置、`finish_reason` 和 `truncated`，判断是否存在短句/低语/技术词被截断。
- 性能指标：记录耗时、RTF、失败样本、模型加载路径和子仓库 commit。

第一轮暂不纳入：

- `temperature` 搜索。当前默认确定性解码更适合做稳定基线。
- speculative decoding / `draft_model`。它主要影响速度和复杂度，第一轮不应混入质量评估。
- diarization、forced aligner、timestamps。当前产品语音输入主链路不依赖这些能力，第一轮不应扩展变量。
- 产品级 streaming。当前 CapsWriter 首要目标是松手后最终文本质量，不先做流式质量调优。
- EQ、AGC、降噪、混响和复杂麦克风仿真。当前阶段不改预处理链路。
- 上游模型结构配置。`config.py` 中的 encoder/decoder 结构参数不是本阶段产品调优入口。

第一轮需要重点观察但不急于修改：

- 低语和低增益样本是否明显触发 `length`、空输出、重复输出或语言误判。
- context 是否改善技术关键词，同时是否引入幻觉或过度纠错。
- 强制语言是否改善中英文单语样本，同时是否伤害中英混合术语。
- 后处理格式化是否掩盖 raw ASR 错误；第一轮报告必须同时保存 raw 文本和最终文本。

### 两个维度正交组织

评测集按两个正交维度组织，不把内容场景和声学条件混为一谈。

| 维度 | 覆盖内容 |
|------|----------|
| 内容场景 | 中文技术讲解、英文技术词汇、英文技术演讲、商业技术口语、中英混合术语 |
| 声学条件 | 正常音量、真实低语、小声/低增益 |

公开数据集很难天然同时满足“真实低语 + 技术内容”。第一轮接受不同数据源分别覆盖内容场景和声学条件，通过统一 manifest 标注来源、语言、场景和声学条件。

### 低语和小声是核心条件

低语、小声说话不是边缘鲁棒性样本，而是 CapsWriter 便携电脑场景的核心评测条件。

原因：

- 用户经常在办公室、会议室、公共空间中压低音量使用。
- 电脑麦克风收音距离和环境噪声不可控。
- 真实低语的发声机制不同于正常说话，不能只靠调小音量模拟。

## 第一轮数据集组合方案

第一轮数据集命名为 `CapsWriter Tech ASR Eval v1`，目标规模控制在 160 条左右；加入低增益派生后，总样本控制在 180 到 200 条。

### 原始样本组合

| 模块 | 数量 | 来源 | 目的 |
|------|------|------|------|
| 中文真实低语 | 40 | AISHELL6-Whisper | 测中文真实低语声学能力 |
| 英文真实低语 | 20 | wTIMIT 或 CHAINS，若下载/授权受阻则暂缓 | 测英文真实低语声学能力 |
| 中文技术讲解 | 35 | Chinese-LiPS 的 `KJ` 科技主题 | 测中文技术讲解和教育演示类内容 |
| 英文技术词汇 | 35 | Tech-Sentences-For-ASR-Training | 测 API、CLI、DevOps、编程词汇 |
| 英文技术演讲 | 20 | TED-LIUM 技术/科学类片段 | 测自然英文技术表达 |
| 商业技术口语 | 10 | Earnings-22 chunked | 测商业会议、口音、电话会风格 |

如果英文真实低语数据源落地成本过高，第一版允许先降级为 140 条：

| 模块 | 数量 |
|------|------|
| 中文真实低语 | 40 |
| 中文技术讲解 | 35 |
| 英文技术词汇 | 35 |
| 英文技术演讲 | 20 |
| 商业技术口语 | 10 |

### 派生样本策略

第一轮只做一类派生：`low_gain`。

| 派生条件 | 处理范围 | 目的 |
|----------|----------|------|
| `low_gain` | 从中文技术讲解、英文技术词汇、英文技术演讲/商业技术口语中抽约 40 条 | 测当前无 AGC 管线下，技术内容音量降低后的退化程度 |

第一轮暂不做 EQ、混响、噪声、复杂麦克风仿真。

原因：

- 第一轮需要先建立可解释基线，避免变量过多。
- 真实低语由真实低语数据集覆盖，不用 EQ 伪装。
- 当前最需要回答的是：现有预处理下，技术内容在低幅度输入时是否明显退化。

## 数据集来源说明

| 数据集 | 用途 | 注意事项 |
|--------|------|----------|
| AISHELL6-Whisper | 中文真实低语与正常语音对照 | CC BY-NC-SA 4.0，仅作为本地研究评测口径 |
| wTIMIT / CHAINS | 英文真实低语候选 | 下载和授权可能比 Hugging Face 数据集麻烦，第一轮可作为可选项 |
| Chinese-LiPS | 中文科技主题讲解 | 优先筛选 `KJ` 科技主题 |
| Tech-Sentences-For-ASR-Training | 英文开发者技术词汇 | 适合 API、CLI、DevOps、编程术语 |
| TED-LIUM | 英文技术/科学演讲 | 只抽少量技术类片段，避免偏离 CapsWriter 场景 |
| Earnings-22 chunked | 商业会议与电话会风格 | 少量加入，用于覆盖商业技术口语 |

### v1 下载落地状态

2026-07-06 已创建 `evals/datasets/capswriter_tech_asr_v1/download_sources.py`，下载策略是只拉 v1 所需的最小源文件或单个 shard，不下载公开数据集全量。

已落地：

- `AISHELL6-Whisper`：2026-07-08 用户 HF 访问申请已通过；已补齐 `AISHELL6-Whisper_info.csv`、`text_sentence`、`w2n.txt`、`metadata.tar.gz`、`test.tar.gz`（约 1.7GiB），用于中文真实低语样本抽取。
- `Tech-Sentences-For-ASR-Training`：小型仓库完整下载，当前本地有 205 条音频和 205 条文本。
- `Chinese-LiPS`：已下载元数据和 `processed_val.zip`，后续优先从 validation split 的 `KJ` 科技主题抽 35 条。
- `TED-LIUM`：已下载 `AudioLLMs/tedlium3_test` 单个 test parquet shard，后续抽 20 条。
- `Earnings-22 chunked`：已下载一个较小 chunked parquet shard，后续抽 10 条。

待处理：

- 中文真实低语是 v1 的关键维度，不能用 `low_gain` 替代；但数据源必须来自 Hugging Face、AI-SHELL 官方平台或作者认可入口，禁止接入来源不可审计、绕过审批或疑似泄露的数据包。
- `TED-LIUM` parquet 首次下载时曾因脚本早期 `local_dir` 逻辑产生 `data/data/` 嵌套落点；脚本已兼容复用该文件，后续抽样脚本应统一搜索实际本地文件或在 manifest 构建前做规范化路径处理。

## 第一轮首要问题

第一轮调优优先解决以下问题。

### 0. 先跑通本地子仓库后端与配置收敛

在建立正式评测结果之前，必须先完成后端接入基线：

- ✅ 2026-07-06 已确认 macOS 服务端实际导入根目录 `mlx-qwen3-asr` 子仓库源码包，实测路径为 `/Users/edgar/programs/CapsWriter-Offline/mlx-qwen3-asr/mlx_qwen3_asr/__init__.py`。
- ✅ 2026-07-06 已确认 `qwen_asr_mlx` 仍通过 CapsWriter 的 `QwenASRMLXEngine` 进入 worker 进程，但主路径已不再走旧 `WorkPipeline` 分片推理和拼接。
- ✅ 2026-07-06 runner 已落地，`qwen_asr_mlx` 主路径以 `AudioFeedPatch` 按时序 feed 同一个 `task_id` 的 package runner；当前 P0 先实现“流式喂音频 + final 离线完整结果”，尚未实现录音过程中提前处理稳定 `InferenceChunk`。
- ✅ 已完成语义收敛：`task_id` 是完整识别任务标识；服务端 worker 执行单元已经统一改名为 `Work`，不再复用 `Task` 表达完整任务语义。
- ✅ 已把当前 `return_timestamps`、`return_chunks`、`max_new_tokens`、`num_draft_tokens`、`verbose` 等推理级入口集中到 `mlx-qwen3-asr` 包内 `CapsWriterRunnerConfig`，避免 CapsWriter 外层和包内两套配置同时生效。
- ✅ 2026-07-06 已把启动预热和 MLX wired memory 落到 package Runner 内：server 默认开启 `enable_startup_prewarm`、`enable_wired_memory`，并以 `wired_memory_limit='auto'` 透传运行意图；Runner 负责预热、读取 active memory、计算 wired limit、调用 `mx.set_wired_limit()` 和记录初始化结果。
- 保留 CapsWriter 外层的最小产品配置入口，避免破坏多后端工厂结构。

完成这一步之后，再开始固定 manifest 的正式基线评测。

### 1. 建立可重复评测闭环

必须先生成固定 manifest，并能在本机稳定跑完。

完成定义：

- manifest 固定记录 `sample_id`、语言、来源、场景、声学条件、音频路径、参考文本。
- 每轮评测样本顺序和抽样结果稳定。
- 输出包含识别文本、CER/WER、耗时和失败样本列表。

### 2. 分清内容错误和声学错误

不能只看总分。需要按条件拆分：

- 中文真实低语。
- 英文真实低语。
- 中文技术讲解。
- 英文技术词汇。
- 英文技术演讲。
- 商业技术口语。
- 技术内容 `low_gain`。

目标是判断当前主要瓶颈到底是技术词汇不认识，还是低语/低音量声学条件导致退化。

### 3. 验证当前管线对低音量是否敏感

当前管线没有 AGC 或响度归一化。`low_gain` 样本用于回答一个具体问题：

> 同一批技术内容，只降低音量后，识别错误是否明显增加？

如果 `low_gain` 相比 `clean` 明显退化，后续才讨论是否需要产品层输入增益或 AGC；第一轮不提前改预处理。

### 4. 评估真实低语可用性

真实低语不是调小音量。第一轮必须单独看真实低语结果。

完成定义：

- 中文真实低语至少有独立 CER 统计。
- 英文真实低语若数据源落地，则独立统计 WER；若暂缓，必须在结果中明确标注缺口。
- 不把 `low_gain` 结果当作真实低语结果。

### 5. 为后续模型和解码参数调优提供基线

第一轮不直接追求最优参数，而是形成可比较基线。

后续调优项包括：

- 1.7B-8bit 与 1.7B-4bit 的质量差异。
- `language` 强制指定与自动识别的差异。
- `context` 对技术词汇识别的帮助。
- 解码参数和 max token 策略对短句、低语、技术词的影响。

这些都必须基于同一套 v1 manifest 做对比。

## 当前不做的事

- 不要求用户自录数据。
- 不新增产品预处理。
- 不做复杂 EQ、噪声、混响增强矩阵。
- 不追求全语言评测。
- 不跑大型全量 benchmark。
- 不把 LibriSpeech/FLEURS 作为本阶段主评测集。

## 后续落地顺序

1. 把服务端依赖切到可验证的本地 `mlx-qwen3-asr` 源码加载方式。
2. 增加最小导入路径检查，确认服务端实际加载根目录子仓库源码。
3. 梳理并收敛 MLX 推理级配置，把 prompt、language、generation、预热和 wired memory 等策略放到子仓库包内集中管理。
4. 创建 `evals/datasets/capswriter_tech_asr_v1/` 数据集目录。
5. 写入数据源清单和 manifest 字段规范。
6. 编写固定抽样脚本，先落地中文低语、中文技术、英文技术三类。
7. 生成 `clean` 与 `low_gain` 样本。
8. 在 `evals/drivers/` 中实现 CapsWriter 后端评测驱动：可构造旧服务端风格的 `Work` 作为临时基线；runner 落地后主路径必须以 `task_id` 调用同一个 package runner。
9. 产出第一份基线报告，回写 `CLAUDE.md` 当前阶段状态。

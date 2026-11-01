# -*- coding: utf-8 -*-
"""
================================================================================
 后端总入口：rag_llm_server/main.py
--------------------------------------------------------------------------------
 本服务共 5 个接口，组成「RTC AI 语音对话 + RAG 知识库问答」完整链路：

  1. POST /getScenes          前端页面加载时调用 → 返回场景列表 + RTC 进房 Token
  2. POST /proxy              前端点「开始/结束通话」→ 本服务用 AK/SK 做 V4 签名，
                              转发给火山 RTC OpenAPI（StartVoiceChat/StopVoiceChat），
                              让 AI Agent 进房与用户通话
  3. POST /api/chat_callback  RTC 服务器主动回调 → 把用户语音识别出的文字交给
                              RAG + LLM，以 SSE 流式返回（CustomLLM 自定义模型模式）
  4. POST /debug/chat         纯文本调试对话（不经过语音，直接测 RAG+LLM 效果）
  5. GET  /debug/rag          调试知识库检索，浏览器直接访问看检索原文

 一次完整语音问答的数据流：
   用户说话 → RTC 语音识别(ASR) → 文字回调到 /api/chat_callback
   → rag_service 检索知识库 → llm_service 流式生成回答（SSE）
   → RTC 语音合成(TTS) → 用户听到 AI 回答
================================================================================
"""
import time
import json
import httpx                          # 异步 HTTP 客户端：用于调火山 OpenAPI
import uvicorn                        # ASGI 服务器：把 FastAPI 跑在 3001 端口
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel        # 定义请求体数据模型（自动参数校验）
from typing import List, Optional
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from config import settings                          # 读取 .env 的全局配置
from services.llm_service import llm_service         # 大模型服务（单例）
from services.token_build import AccessToken, PRIVILEGES  # RTC Token 生成器
from services.utils import Signer                    # V4 签名器
from services.rag_service import rag_service         # 知识库检索服务（单例）

# 【重要】必须先执行 load_dotenv()，把 .env 里的变量注入系统环境，
# 后面 config.py 里 os.getenv() 才能读到值（与 config.py 各调一次没有副作用）
load_dotenv()

# 创建 FastAPI 应用实例
app = FastAPI()

# 跨域中间件：允许前端(localhost:3000)跨域访问本服务(localhost:3001)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 生产环境建议改成前端的具体地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 接口 1：获取场景列表（前端启动时调用）
# 作用：告诉前端"有哪些 AI 助手场景"，并签发一张 RTC 进房"门票"(Token)
# ============================================================================
@app.post("/getScenes")
async def get_scenes(request: Request):
    # 房间号和用户名目前写死（与前端约定的演示账号）
    room_id = "ChatRoom01"      # RTC 房间 ID：用户和 AI Agent 都进这个房间
    user_id = "Huoshan01"       # 用户在 RTC 房间里的身份标识

    # --- 签发 RTC Token（进房门票） ---
    # 参数：应用 ID、应用密钥、房间号、用户名（必须与控制台配置一致）
    token_builder = AccessToken(
        settings.RTC_APP_ID, settings.RTC_APP_KEY, room_id, user_id
    )
    # 授予权限：订阅流(听 AI 说话) + 发布流(自己说话)
    # expire_timestamp=0 表示该权限跟随 Token 整体过期时间
    token_builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
    token_builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
    # Token 整体有效期：当前时间 + 24 小时
    token_builder.expire_time(int(time.time()) + 3600 * 24)
    token = token_builder.serialize()   # 序列化成最终字符串 Token

    # 构造返回结构（模拟火山 OpenAPI 标准响应格式，前端按此格式解析）
    return {
        "ResponseMetadata": {"Action": "getScenes"},
        "Result": {
            "scenes": [
                {
                    "scene": {
                        "id": "Custom",        # 场景 ID，前端据此做特殊处理
                        "name": "自定义助手",   # 界面显示的场景名
                        "botName": "AiAgent",  # AI 助手在 RTC 房间里的 UserId
                        "icon": "https://lf3-rtc-demo.volccdn.com/obj/rtc-aigc-assets/DoubaoAvatar.png",
                        "isInterruptMode": True,   # 支持打断（AI 说话时可插话）
                        "isVision": False,         # 不开摄像头视觉
                        "isScreenMode": False,     # 不开屏幕共享
                        "isAvatarScene": None,     # 无数字人
                        "avatarBgUrl": None,
                    },
                    "rtc": {
                        "AppId": settings.RTC_APP_ID,   # RTC 应用 ID
                        "RoomId": room_id,
                        "UserId": user_id,
                        # 【Bug 修复 1】原代码这里写死了一个过期 Token 字符串，
                        # 前端拿它进房必然报 token_error！现在改用上面刚生成的
                        # token（用 .env 里的 AppId/AppKey 签发，24 小时有效）
                        "Token": token,
                    },
                    # 空配置占位，前端原样透传；真正生效的配置在 /proxy 里下发
                    "VoiceChat": {},
                }
            ]
        },
    }


# ============================================================================
# 接口 2：代理转发火山 RTC OpenAPI（核心配置下发 + 签名转发）
# 为什么需要代理？AK/SK 是绝密凭据，绝不能放到前端代码里，
# 必须由后端签名后转发 —— 这是安全设计。
# ============================================================================
@app.post("/proxy")
async def proxy(request: Request):
    # 从 URL 查询串取动作名，例如 ?Action=StartVoiceChat&Version=2025-06-01
    action = request.query_params.get("Action")
    # 【Bug 修复 2】原默认 "2024-12-01" 已过期，火山 RTC OpenAPI 当前版本
    # 是 2025-06-01，版本不对接口会直接报错
    version = request.query_params.get("Version", "2025-06-01")

    # 打印前端发来的原始数据，方便调试观察
    try:
        incoming_body = await request.json()
        print(f"DEBUG: 收到前端请求 {action}, Body: {incoming_body}")
    except:
        pass

    # --- 通话参数配置 ---
    # 【Bug 修复 3】原来硬编码 target_app_id = "6933e1446a6de10173e1e306"
    # （旧示例的 AppId），与你自己的 RTC 应用 "6ade0ba010d5bc04c9b8e6f2"
    # 不匹配 → 火山引擎会拒绝请求。现在统一从 .env 读取，
    # 保证 /getScenes 和 /proxy 用同一个 AppId
    target_app_id = settings.RTC_APP_ID
    target_room_id = "ChatRoom01"   # 必须与 /getScenes 的 room_id 一致
    target_user_id = "Huoshan01"    # 必须与 /getScenes 的 user_id 一致

    request_body = {}

    # 回调地址打印（SERVER_URL 必须是火山服务器能访问到的公网地址）
    print(f"RTCCCCC  callback {settings.SERVER_URL}/api/chat_callback")

    if action == "StartVoiceChat":
        # 开始语音通话：告诉火山"派一个 AI Agent 进 RTC 房间"
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": "ChatTask01",     # 任务 ID（结束通话要用同一个）
            "AgentConfig": {
                "TargetUserId": [target_user_id],  # AI 对谁说话
                "WelcomeMessage": "我是懂小智，你的专属课程顾问，有什么问题尽管问我吧，我比懂王更强",
                "UserId": "AiAgent",               # AI Agent 在房间里的身份
                # 通话状态变化时回调通知（开始说话/结束说话等）
                "EnableConversationStateCallback": True,
            },
            "Config": {
                # --- ASR：语音识别配置（把用户说的话转成文字） ---
                "ASRConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "Mode": "smallmodel",  # 小模型模式（响应快）
                        "AppId": "7077298582", # 火山语音技术 AppId
                        "Cluster": "volcengine_streaming_common",  # 流式识别集群
                    },
                },
                # --- TTS：语音合成配置（把 AI 回答转成语音播放） ---
                "TTSConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "app": {"appid": "7077298582", "cluster": "volcano_tts"},
                        "audio": {
                            "voice_type": "BV001_streaming",  # 音色（通用女声）
                            "speed_ratio": 1,   # 语速
                            "pitch_ratio": 1,   # 音调
                            "volume_ratio": 1,  # 音量
                        },
                    },
                },
                # --- LLM：大模型配置（CustomLLM = 用我们自己的问答服务） ---
                "LLMConfig": {
                    "Mode": "CustomLLM",   # RTC 把识别的文字 POST 到下面的 Url，
                                           # 我们返回 SSE 流作为 AI 回答
                    "Url": f"{settings.SERVER_URL}/api/chat_callback",
                    "Method": "POST",
                    # 回调地址是 https 还是 http（火山要求显式声明）
                    "ApiType": "https"
                    if str(settings.SERVER_URL).startswith("https")
                    else "http",
                },
                "InterruptMode": 0,   # 打断模式
            },
        }
    elif action == "StopVoiceChat":
        # 结束通话：让 AI Agent 退出房间（TaskId 必须与开始时一致）
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": "ChatTask01",
        }
    else:
        # 其他未知 Action：原样透传前端内容
        request_body = incoming_body

    # --- V4 签名并转发到火山 RTC OpenAPI ---
    host = "rtc.volcengineapi.com"
    # 组装"待签名请求"：Signer 会根据这些信息算出 Authorization 头
    open_api_request_data = {
        "method": "POST",
        "path": "/",
        "params": {"Action": action, "Version": version},  # 拼在 URL 上
        "headers": {"Host": host, "Content-Type": "application/json"},
        "body": request_body,   # 请求体（序列化后参与签名）
    }

    # AK/SK：火山主账号访问密钥，必须有 RTC OpenAPI 调用权限
    account_config = {"accessKeyId": settings.VOLC_AK, "secretKey": settings.VOLC_SK}

    # service="rtc"：签名里要声明访问哪个火山服务
    signer = Signer(open_api_request_data, "rtc")
    signer.add_authorization(account_config)   # 写入 Authorization / X-Date 等头

    url = f"https://{host}?Action={action}&Version={version}"

    # 【Bug 修复 4 · 关键】签名的字节和实际发送的字节必须完全一致！
    # Signer 内部用 json.dumps(body) 计算哈希，如果 httpx 的 json= 参数用
    # 不同的序列化格式，火山端校验签名就会失败。所以这里手动序列化一次，
    # 签名和发送共用同一份字符串
    body_str = json.dumps(request_body) if request_body else ""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers=open_api_request_data["headers"],  # 已含 Authorization 签名头
            content=body_str.encode("utf-8"),          # 【修复】content= 精确控制字节
            timeout=30.0,
        )
        result = resp.json()
        # 成功会返回 TaskId，失败会有 Error 信息
        print(f"DEBUG: 火山引擎返回结果: {result}")
        return result


# ============================================================================
# 接口 3：RTC 服务器回调（整条链路的"大脑"接入口）
# 流程：用户说话 → RTC 识别成文字 → RTC 把消息 POST 到这里
#       → RAG 检索 + LLM 流式生成 → SSE 返回给 RTC → TTS 播放
# ============================================================================
@app.post("/api/chat_callback")
async def chat_callback(request: Request):
    # 解析 RTC 发来的 JSON；失败返回空文本（RTC 会跳过本轮回答）
    try:
        data = await request.json()
    except:
        return {"text": ""}

    print(f"======================== 流式请求", data)

    # 对话历史（RTC 会带上之前的问答，方便模型理解上下文）
    messages = data.get("messages", [])

    # 校验：消息非空，且最后一条必须是用户说的（忽略系统/agent 自身消息，
    # 否则 AI 会自己跟自己对话，死循环刷 Token）
    if not messages or messages[-1].get("role") != "user":
        print("⚠️ 忽略：非用户主动发言")
        return {"text": ""}

    # --- SSE 生成器：边生成边推送，实现"打字机"式流式回答 ---
    async def generate_sse():
        # 1. 用用户最新这句话去知识库检索参考资料
        rag_content = await rag_service.retrieve(messages[-1].get("content", ""))

        # 2. 调大模型流式生成（RAG 上下文会拼进 system 提示词）
        stream_iterator = llm_service.chat_stream(messages, rag_content)

        # 3. 逐块输出。Ark SDK 的 chunk 是 ChatCompletionChunk 对象，
        #    model_dump_json() 转成 JSON 字符串 —— 与 OpenAI 格式兼容，
        #    这正是 RTC CustomLLM 模式要求的格式
        for chunk in stream_iterator:
            if chunk:
                chunk_json = chunk.model_dump_json()
                # SSE 协议格式：每条消息 "data: " 开头、空行结尾
                yield f"data: {chunk_json}\n\n"

        # 4. 必须发结束符，RTC 收到 [DONE] 才认为回答完毕
        yield "data: [DONE]\n\n"

    # 返回流式响应（media_type 必须是 text/event-stream）
    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ============================================================================
# 接口 4：纯文本调试对话（不经过语音，单独验证 RAG + LLM 效果）
# ============================================================================
class ChatMessage(BaseModel):
    role: str      # "user"（用户）或 "assistant"（AI）
    content: str   # 消息内容


class DebugRequest(BaseModel):
    history: Optional[List[ChatMessage]] = []   # 历史对话（可选）
    question: str                               # 本次提问


@app.post("/debug/chat")
async def debug_chat(request: DebugRequest):
    # 把 Pydantic 模型转成 SDK 需要的 dict 列表
    current_messages = []
    for msg in request.history:
        current_messages.append({"role": msg.role, "content": msg.content})
    # 追加用户最新问题
    current_messages.append({"role": "user", "content": request.question})

    # --- 纯文本流式生成器 ---
    async def generate_text():
        full_ai_response = ""
        total_usage = None

        # 1. 记录知识库检索耗时
        start_t = time.time()
        rag_content = await rag_service.retrieve(request.question)
        rag_duration = time.time() - start_t
        print(f"DEBUG: 知识库查询耗时: {rag_duration:.2f}s")

        # 2. 记录 LLM 调用耗时
        llm_start_t = time.time()
        stream = llm_service.chat_stream(current_messages, rag_content)

        # 逐块取出回答文本：累积完整回答 + 边生成边推送
        for chunk in stream:
            if chunk and chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    content = delta.content
                    full_ai_response += content   # 累积 AI 完整回答
                    yield content
            # 最后一个 chunk 会带 Token 用量统计
            if hasattr(chunk, "usage") and chunk.usage:
                total_usage = chunk.usage

        llm_duration = time.time() - llm_start_t
        print(f"DEBUG: LLM 调用耗时: {llm_duration:.2f}s")

        if total_usage:
            print(
                f"🎫 Token 统计: Total={total_usage.total_tokens} "
                f"(P:{total_usage.prompt_tokens}, C:{total_usage.completion_tokens})"
            )

        # 流结束后打印可直接复用的 history 结构（下次调试直接复制粘贴）
        new_history = []
        for m in request.history:
            new_history.append({"role": m.role, "content": m.content})
        new_history.append({"role": "user", "content": request.question})
        new_history.append({"role": "assistant", "content": full_ai_response})

        print("\n" + "=" * 50)
        print("🐞 调试完成！以下是可用于下次请求的 history 结构：")
        print(json.dumps({"history": new_history}, ensure_ascii=False, indent=2))
        print("=" * 50 + "\n")

    # 纯文本流式响应（curl / Postman / 前端 fetch 都能直接看）
    return StreamingResponse(generate_text(), media_type="text/plain")


# ============================================================================
# 接口 5：知识库检索调试（GET 请求，浏览器直接打开就能测）
# ============================================================================
@app.get("/debug/rag")
async def debug_rag(query: str):
    """
    用法：http://127.0.0.1:3001/debug/rag?query=你的问题
    作用：直接看知识库检索出来的原文，验证 RAG 配置是否正确
    """
    if not query:
        return {"error": "请提供 query 参数"}

    print(f"🔍 [Debug] 正在检索知识库: {query}")
    context = await rag_service.retrieve(query)

    return {
        "query": query,
        "retrieved_context": context,   # 检索到的知识原文
        "length": len(context) if context else 0,
        "status": "success" if context else "no_results_or_error",
    }


# ============================================================================
# 服务启动入口：python main.py 直接运行
# ⚠️ 注意：端口 3001 与 server_python 冲突！两个服务只能同时跑一个
# ============================================================================
if __name__ == "__main__":
    print(f"🚀 Server running at {settings.SERVER_URL}")
    uvicorn.run(
        "main:app",          # 指向本文件的 app 对象
        host="0.0.0.0",      # 监听所有网卡（方便外部访问）
        port=3001,           # 端口（前端代理的目标端口）
        reload=True,         # 代码改动自动重启（开发模式）
        reload_dirs=[".", "services"],   # 监听这些目录的文件变化
        reload_excludes=[
            "*/__pycache__/*",   # 排除缓存文件，防止误触发重启
            "*.pyc",
            ".venv/*",
            "*/.venv/*",
        ],
    )

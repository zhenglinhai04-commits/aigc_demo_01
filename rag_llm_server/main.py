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
================================================================================
"""
import asyncio
import json
import logging
import time
from typing import List, Optional
from dotenv import load_dotenv

# 【修复 1】必须在导入 settings/业务模块之前加载 .env，确保模块级环境变量正常注入
load_dotenv()

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from services.llm_service import llm_service
from services.rag_service import rag_service
from services.token_build import PRIVILEGES, AccessToken
from services.utils import Signer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rag_llm_server")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 异步/同步流式适配辅助函数（解决同步 Stream 阻塞 AsyncIO 事件循环）
# ============================================================================
async def iterate_sync_stream_in_thread(sync_iterator):
    """
    【修复 2】在后台 Worker 线程中迭代同步 SDK 生成器，
    通过 asyncio.Queue 将数据块推回主事件循环，彻底避免阻塞并发连接。
    """
    queue = asyncio.Queue(maxsize=30)
    loop = asyncio.get_running_loop()
    sentinel = object()

    def _producer():
        try:
            for item in sync_iterator:
                asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
        except Exception as e:
            logger.error(f"LLM 流式拉取异常: {e}", exc_info=True)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(sentinel), loop).result()

    loop.run_in_executor(None, _producer)

    while True:
        item = await queue.get()
        if item is sentinel:
            break
        yield item


# ============================================================================
# 接口 1：获取场景列表（前端启动时调用）
# ============================================================================
@app.post("/getScenes")
async def get_scenes(request: Request):
    # 【修复 3】支持动态传参并提供默认值，避免多端联调写死冲突
    try:
        body = await request.json()
    except Exception:
        body = {}

    room_id = body.get("roomId", "ChatRoom01")
    user_id = body.get("userId", "Huoshan01")

    token_builder = AccessToken(
        settings.RTC_APP_ID, settings.RTC_APP_KEY, room_id, user_id
    )
    token_builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
    token_builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
    token_builder.expire_time(int(time.time()) + 3600 * 24)
    token = token_builder.serialize()

    return {
        "ResponseMetadata": {"Action": "getScenes"},
        "Result": {
            "scenes": [
                {
                    "scene": {
                        "id": "Custom",
                        "name": "自定义助手",
                        "botName": "AiAgent",
                        "icon": "https://lf3-rtc-demo.volccdn.com/obj/rtc-aigc-assets/DoubaoAvatar.png",
                        "isInterruptMode": True,
                        "isVision": False,
                        "isScreenMode": False,
                        "isAvatarScene": None,
                        "avatarBgUrl": None,
                    },
                    "rtc": {
                        "AppId": settings.RTC_APP_ID,
                        "RoomId": room_id,
                        "UserId": user_id,
                        "Token": token,
                    },
                    "VoiceChat": {},
                }
            ]
        },
    }


# ============================================================================
# 接口 2：代理转发火山 RTC OpenAPI（核心配置下发 + 签名转发）
# ============================================================================
@app.post("/proxy")
async def proxy(request: Request):
    action = request.query_params.get("Action")
    version = request.query_params.get("Version", "2025-06-01")

    # 【修复 4】严谨解析请求体，规避裸 except 隐患
    try:
        incoming_body = await request.json()
    except Exception as e:
        logger.warning(f"解析 /proxy 请求 JSON 失败或体为空: {e}")
        incoming_body = {}

    target_app_id = settings.RTC_APP_ID
    target_room_id = incoming_body.get("RoomId", "ChatRoom01")
    target_user_id = incoming_body.get("UserId", "Huoshan01")
    target_task_id = incoming_body.get("TaskId", "ChatTask01")

    request_body = {}
    logger.info(f"RTC Callback Target: {settings.SERVER_URL}/api/chat_callback")

    if action == "StartVoiceChat":
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": target_task_id,
            "AgentConfig": {
                "TargetUserId": [target_user_id],
                "WelcomeMessage": "我是懂小智，你的专属课程顾问，有什么问题尽管问我吧，我比懂王更强",
                "UserId": "AiAgent",
                "EnableConversationStateCallback": True,
            },
            "Config": {
                "ASRConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "Mode": "smallmodel",
                        "AppId": "7077298582",
                        "Cluster": "volcengine_streaming_common",
                    },
                },
                "TTSConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "app": {"appid": "7077298582", "cluster": "volcano_tts"},
                        "audio": {
                            "voice_type": "BV001_streaming",
                            "speed_ratio": 1,
                            "pitch_ratio": 1,
                            "volume_ratio": 1,
                        },
                    },
                },
                "LLMConfig": {
                    "Mode": "CustomLLM",
                    "Url": f"{settings.SERVER_URL}/api/chat_callback",
                    "Method": "POST",
                    "ApiType": "https"
                    if str(settings.SERVER_URL).startswith("https")
                    else "http",
                },
                "InterruptMode": 0,
            },
        }
    elif action == "StopVoiceChat":
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": target_task_id,
        }
    else:
        request_body = incoming_body

    host = "rtc.volcengineapi.com"
    open_api_request_data = {
        "method": "POST",
        "path": "/",
        "params": {"Action": action, "Version": version},
        "headers": {"Host": host, "Content-Type": "application/json"},
        "body": request_body,
    }

    account_config = {"accessKeyId": settings.VOLC_AK, "secretKey": settings.VOLC_SK}
    signer = Signer(open_api_request_data, "rtc")
    signer.add_authorization(account_config)

    url = f"https://{host}?Action={action}&Version={version}"

    # 【修复 5】严格规整序列化格式（去除空格转义），保障签名字符串与网络传输字节完全同质
    body_str = (
        json.dumps(request_body, ensure_ascii=False, separators=(",", ":"))
        if request_body
        else ""
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=open_api_request_data["headers"],
                content=body_str.encode("utf-8"),
                timeout=30.0,
            )
            result = resp.json()
            logger.info(f"火山引擎返回结果 [{action}]: {result}")
            return result
    except httpx.HTTPError as e:
        logger.error(f"调用火山 OpenAPI 失败: {e}", exc_info=True)
        return {"ResponseMetadata": {"Error": {"Code": "ProxyHttpError", "Message": str(e)}}}


# ============================================================================
# 接口 3：RTC 服务器回调（整条链路的大脑接入口）
# ============================================================================
@app.post("/api/chat_callback")
async def chat_callback(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        logger.warning(f"RTC 回调 JSON 解析错误: {e}")
        return {"text": ""}

    messages = data.get("messages", [])

    if not messages or messages[-1].get("role") != "user":
        logger.info("⚠️ 忽略：非用户发言回调")
        return {"text": ""}

    async def generate_sse():
        try:
            user_question = messages[-1].get("content", "")
            rag_content = await rag_service.retrieve(user_question)

            # 在线程池运行同步 LLM 流，避免堵塞事件循环
            sync_stream = llm_service.chat_stream(messages, rag_content)
            async for chunk in iterate_sync_stream_in_thread(sync_stream):
                if chunk:
                    chunk_json = chunk.model_dump_json()
                    yield f"data: {chunk_json}\n\n"
        except Exception as err:
            logger.error(f"SSE 流式生成异常: {err}", exc_info=True)
        finally:
            # 【修复 6】无论流式过程是否出现异常，必定投递 [DONE]，防止 RTC 服务端悬挂超时
            yield "data: [DONE]\n\n"

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
# 接口 4：纯文本调试对话
# ============================================================================
class ChatMessage(BaseModel):
    role: str
    content: str


class DebugRequest(BaseModel):
    history: Optional[List[ChatMessage]] = []
    question: str


@app.post("/debug/chat")
async def debug_chat(request: DebugRequest):
    current_messages = [{"role": msg.role, "content": msg.content} for msg in request.history]
    current_messages.append({"role": "user", "content": request.question})

    async def generate_text():
        full_ai_response = ""
        total_usage = None

        start_t = time.time()
        rag_content = await rag_service.retrieve(request.question)
        logger.info(f"知识库查询耗时: {time.time() - start_t:.2f}s")

        llm_start_t = time.time()
        sync_stream = llm_service.chat_stream(current_messages, rag_content)

        async for chunk in iterate_sync_stream_in_thread(sync_stream):
            if chunk and chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    content = delta.content
                    full_ai_response += content
                    yield content
            if hasattr(chunk, "usage") and chunk.usage:
                total_usage = chunk.usage

        logger.info(f"LLM 调用耗时: {time.time() - llm_start_t:.2f}s")
        if total_usage:
            logger.info(
                f"Token 统计: Total={total_usage.total_tokens} "
                f"(P:{total_usage.prompt_tokens}, C:{total_usage.completion_tokens})"
            )

        new_history = [{"role": m.role, "content": m.content} for m in request.history]
        new_history.append({"role": "user", "content": request.question})
        new_history.append({"role": "assistant", "content": full_ai_response})
        logger.info(f"下次请求可用 history:\n{json.dumps({'history': new_history}, ensure_ascii=False, indent=2)}")

    return StreamingResponse(generate_text(), media_type="text/plain")


# ============================================================================
# 接口 5：知识库检索调试
# ============================================================================
@app.get("/debug/rag")
async def debug_rag(query: str):
    if not query:
        return {"error": "请提供 query 参数"}

    logger.info(f"正在检索知识库: {query}")
    context = await rag_service.retrieve(query)

    return {
        "query": query,
        "retrieved_context": context,
        "length": len(context) if context else 0,
        "status": "success" if context else "no_results_or_error",
    }


# ============================================================================
# 服务启动入口
# ============================================================================
if __name__ == "__main__":
    logger.info(f"Server configured for {settings.SERVER_URL}")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=3001,
        reload=True,
        reload_dirs=[".", "services"],
        reload_excludes=[
            "*/__pycache__/*",
            "*.pyc",
            ".venv/*",
            "*/.venv/*",
        ],
    )
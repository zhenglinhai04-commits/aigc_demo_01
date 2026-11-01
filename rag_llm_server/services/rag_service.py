# -*- coding: utf-8 -*-
"""
RAG 知识库检索服务：调用火山引擎知识库 API，把用户问题匹配出最相关的知识原文。
链路位置：RTC 回调拿到用户问题 → 本服务检索 → 结果拼进 LLM 的 system 提示词
"""
import os
import json
import httpx
from config import settings
from services.utils import Signer


class RagService:
    """知识库检索：V4 签名调 search_knowledge 接口，返回拼接后的上下文文本"""

    def __init__(self):
        # 1. 主账号 AK/SK（V4 签名用，与调 RTC OpenAPI 的是同一套）
        self.ak = settings.VOLC_AK
        self.sk = settings.VOLC_SK

        # 2. 知识库专属配置（支持 .env 覆盖，未配置则用默认值）
        self.collection_name = os.getenv("KB_COLLECTION_NAME", "dw_ai")  # 知识库集合名
        self.project_name = os.getenv("KB_PROJECT_NAME", "default")      # 项目名
        # 火山主账号 ID（知识库接口必填，放在 V-Account-Id 请求头里）
        self.account_id = os.getenv("VOLC_ACCOUNT_ID", "kb-2580e8a6357082fb")

        # 3. 知识库服务固定配置
        self.host = "api-knowledgebase.mlp.cn-beijing.volces.com"  # 服务域名
        self.region = "cn-north-1"   # 签名区域
        self.service = "air"         # 签名服务名（知识库固定为 air，注意不是 rtc！）

    async def retrieve(self, query: str) -> str:
        """
        检索知识库
        :param query: 用户问题
        :return: 拼接后的知识文本（检索失败返回空串，不阻塞主链路）
        """
        # 前置校验：密钥缺失直接返回空，并打印醒目提示
        if not self.ak or not self.sk or not self.account_id:
            print(f"⚠️ [RagService] 配置缺失: 请检查 VOLC_AK, VOLC_SK, "
                  f"VOLC_ACCOUNT_ID(当前: {self.account_id})")
            return ""

        path = "/api/knowledge/collection/search_knowledge"

        # 请求体：在指定知识库里搜 query，取相关度最高的一条
        body = {
            "project": self.project_name,
            "name": self.collection_name,
            "query": query,
            "limit": 1,   # 取 Top1（可按需调大）
            "pre_processing": {   # 检索前处理
                "need_instruction": True,
                "return_token_usage": True,
                "messages": [{"role": "user", "content": query}]
            },
            "post_processing": {  # 检索后处理：返回附件链接
                "get_attachment_link": True
            }
        }

        # 请求头：V-Account-Id 是知识库接口的硬性要求，缺失会报权限错误
        headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "V-Account-Id": self.account_id
        }

        # 组装"待签名请求"交给 Signer（注意 service/region 与 RTC 不同）
        request_data = {
            "method": "POST",
            "path": path,
            "headers": headers,
            "body": body,
            "params": {}
        }

        try:
            # 计算签名（Signer 会把 Authorization / X-Date 等头写回 headers）
            signer = Signer(request_data, service=self.service, region=self.region)
            signer.add_authorization({"accessKeyId": self.ak, "secretKey": self.sk})

            url = f"http://{self.host}{path}"

            # 【Bug 修复】与 main.py 同源问题：签名字节必须和发送字节完全一致。
            # 手动 json.dumps 序列化，用 content= 精确发送（不能用 json= 参数）
            body_str = json.dumps(body) if body else ""

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    headers=request_data["headers"],   # 已含签名头
                    content=body_str.encode("utf-8"),  # 【修复】原为 json=body
                    timeout=10.0,
                )

            # HTTP 层失败：打印状态码和响应体，返回空串
            if resp.status_code != 200:
                print(f"❌ [RagService] 请求失败: {resp.status_code}, {resp.text}")
                return ""

            data = resp.json()

            # --- 结果提取 ---
            # 级联 .get() 防止中间某层 key 缺失直接抛异常
            result_list = data.get("data", {}).get("result_list", [])

            # 取出每条结果的 content 字段（过滤空内容）
            contents = [item.get("content", "") for item in result_list
                        if item.get("content")]

            if not contents:
                print(f"⚠️ [RagService] 未检索到匹配的知识内容")
                return ""

            # 多条知识用空行分隔后整体返回（LLM 靠分隔符区分知识块）
            context_text = "\n\n".join(contents)

            print(f"✅ [RagService] 成功提取 {len(contents)} 条知识内容")
            print(f"【传给LLM的,上下文内容】:\n{context_text}")
            return context_text

        except Exception as e:
            # 任何异常都不阻塞主链路：宁可没上下文，也要让 LLM 正常回答
            print(f"❌ [RagService] 异常: {e}")
            return ""


# 全局单例：main.py 直接 from services.rag_service import rag_service
rag_service = RagService()

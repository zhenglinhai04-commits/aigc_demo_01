# -*- coding: utf-8 -*-
"""
大模型服务：封装火山方舟（Ark）SDK 的流式调用。
链路位置：RTC 回调 → rag_service 检索知识库 → 本服务流式生成回答
"""
from volcenginesdkarkruntime import Ark   # 火山方舟 SDK（OpenAI 兼容风格）
from config import settings


class LLMService:
    """大模型服务：拼接提示词（人设 + RAG 知识）并流式调用方舟模型"""

    def __init__(self):
        # 鉴权优先级：ARK_API_KEY > 主账号 AK/SK（IAM 签名兜底）> 置空报错
        api_key = settings.ARK_API_KEY
        if api_key:
            self.client = Ark(
                base_url="https://ark.cn-beijing.volces.com/api/v3",  # 方舟 V3 API 地址
                api_key=api_key,
                timeout=1800,   # 超长超时（秒）：给长回答和慢网络留余量
            )
        elif settings.VOLC_AK and settings.VOLC_SK:
            # 【兜底方案】没有方舟 API Key 时，用主账号 AK/SK 做 IAM 签名鉴权，
            # 效果相同（这就是 server_python 里 ArkV3 模式的鉴权方式）
            try:
                self.client = Ark(
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    ak=settings.VOLC_AK,
                    sk=settings.VOLC_SK,
                    timeout=1800,
                )
                print("✅ [LLMService] 使用 AK/SK 签名鉴权连接方舟")
            except Exception as e:
                print(f"❌ [LLMService] AK/SK 鉴权初始化失败: {e}")
                self.client = None
        else:
            # 【防御】两者都没配置时不让服务启动崩溃，只置空并提示；
            # 调用对话接口时会返回"服务配置错误"
            print("⚠️ [LLMService] 未配置 ARK_API_KEY 或 AK/SK，LLM 功能不可用")
            self.client = None

    def chat_stream(self, history_messages: list, rag_context: str = ""):
        """
        流式对话：yield 一个个 chunk，调用方边收边推给用户
        :param history_messages: 对话历史 [{"role","content"},...]，含用户最新提问
        :param rag_context: rag_service 检索出的知识库文本
        """
        if not self.client:
            yield "服务配置错误"
            return

        # --- 1. 系统提示词：定义 AI 人设「懂小智」（毒舌课程顾问） ---
        # 想改 AI 的性格/话术，直接改这段文字即可
        system_content = """
        # 角色
        你是【懂小智】，AI培训机构“懂王”的金牌顾问。你的老板是懂王老师，你的说话风格：**硬核、清醒、毒舌但热血**。

        # 核心任务
        1. 依据【参考知识库】回答咨询。
        2. 知识库有内容：直接复用库里那些“带劲”的话，不要美化成废话。
        3. 知识库没内容：执行【拦截话术】。

        # 行为准则
        - **不废话**：用短句，多用祈使句。不要说“理解您的意思”，直接给答案。
        - **反幻觉**：严禁编造价格和课程。库里没有，就说：“抱歉，这块信息库还没更新，留个联系方式，我让老师直接跟你对线。”
        - **价值观**：认同“工资高才是硬道理”、“技术是狗屎，工资是真理”。

        # 常用金句（优先从库里取）
        - “你只是老了，不是死了。”
        - “学技术不是目的，高工资才是硬道理。”
        - “我命由我不由天。”
                """.strip()

        # --- 2. 拼装最终 system 提示词：人设 + RAG 知识 ---
        system_blocks = [system_content]
        if rag_context:
            # 用明确定界符包住知识库内容，帮模型快速区分"人设"和"资料"
            system_blocks.append(f"### 参考知识库（绝对准则）\n{rag_context.strip()}")

        final_system_prompt = "\n\n".join(system_blocks)

        # --- 3. 组装消息序列：system 打头，后面接历史对话（含最新提问） ---
        messages = [{"role": "system", "content": final_system_prompt}]
        messages.extend(history_messages)

        try:
            print(f"🚀 发起流式调用 (Endpoint: {settings.ARK_ENDPOINT_ID})")

            stream = self.client.chat.completions.create(
                model=settings.ARK_ENDPOINT_ID,   # 接入点 ID（ep-xxx）或模型名
                messages=messages,
                temperature=0.3,   # 低随机性：回答更严谨贴合知识库
                stream=True,       # 流式输出：边生成边返回，降低首字延迟
                # 让最后一个 chunk 携带 Token 用量统计（计费观测用）
                stream_options={"include_usage": True},
            )

            for chunk in stream:   # 逐块产出给上层
                yield chunk

        except Exception as e:
            print(f"❌ LLM 调用失败: {e}")
            yield None   # 异常时 yield None，上层判断后跳过


# 全局单例：main.py 直接 from services.llm_service import llm_service
llm_service = LLMService()

# -*- coding: utf-8 -*-
"""
配置中心：从本目录的 .env 文件读取所有密钥配置。
其他文件统一通过 `from config import settings` 使用，不要散落各处 os.getenv。
"""
import os
from dotenv import load_dotenv

# 加载 .env 文件，把里面的 KEY=VALUE 注入系统环境变量
load_dotenv()


class Config:
    # --- 火山引擎主账号访问密钥（V4 签名调 OpenAPI：RTC / 知识库 / Ark 兜底） ---
    VOLC_AK = os.getenv("VOLC_ACCESS_KEY")   # Access Key ID
    VOLC_SK = os.getenv("VOLC_SECRET_KEY")   # Secret Access Key

    # --- 火山方舟大模型配置 ---
    ARK_ENDPOINT_ID = os.getenv("ARK_ENDPOINT_ID")   # 推理接入点 ID（ep-xxx 或模型名）
    ARK_API_KEY = os.getenv("ARK_API_KEY")           # 方舟 API Key（可空，用 AK/SK 兜底）

    # --- RTC 实时音视频应用配置（进房 Token 签发 / 开启语音通话） ---
    RTC_APP_ID = os.getenv("RTC_APP_ID")     # RTC 应用 ID
    RTC_APP_KEY = os.getenv("RTC_APP_KEY")   # RTC 应用密钥（签 Token 用，勿外泄）

    # --- 本服务对外地址（必须是火山服务器能访问到的公网 URL） ---
    SERVER_URL = os.getenv("SERVER_URL")


# 全局单例：其他文件直接 from config import settings 使用
settings = Config()

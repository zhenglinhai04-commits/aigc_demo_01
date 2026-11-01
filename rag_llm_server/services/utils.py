# -*- coding: utf-8 -*-
"""
签名与工具函数。
核心是 Signer 类：实现火山引擎 V4 签名算法（HMAC-SHA256）。
所有调火山 OpenAPI 的请求（RTC 开启/结束通话、知识库检索）都必须先签名。

V4 签名流程一览：
  1. 组装规范请求 CanonicalRequest（方法/路径/参数/头/Body哈希 按格式拼接）
  2. 对规范请求整体求 SHA256 → StringToSign
  3. 用 SK 逐层派生签名密钥：SK → 日期 → 区域 → 服务 → "request"
  4. 用派生密钥对 StringToSign 做 HMAC-SHA256 → 最终签名
  5. 把签名拼成 Authorization 头塞回 headers
"""
import os
import json
import hashlib
import hmac
import datetime
from fastapi.responses import JSONResponse


class Signer:
    """火山引擎 V4 请求签名器"""

    def __init__(self, request_data, service, region='cn-north-1'):
        # 从调用方传入的"请求描述"里解构出各部分
        self.method = request_data.get('method', 'POST').upper()   # HTTP 方法
        self.path = request_data.get('path', '/')                  # 请求路径
        self.params = request_data.get('params', {})               # URL 查询参数
        self.headers = request_data.get('headers', {})             # 请求头（签名后回写）
        self.body = request_data.get('body', {})                   # 请求体
        self.service = service    # 服务名：RTC 传 "rtc"，知识库传 "air"
        self.region = region      # 区域：cn-north-1（北京）

    def add_authorization(self, account_config):
        """计算签名并把 Authorization 头写入 self.headers（原地修改）"""
        ak = account_config.get('accessKeyId')
        sk = account_config.get('secretKey')
        if not ak or not sk:
            return   # 缺密钥直接跳过（调用会因无鉴权失败，方便暴露问题）

        # --- 第 1 步：准备 UTC 时间（火山要求 GMT 时间，用本地时区会导致签名过期） ---
        now = datetime.datetime.utcnow()
        date = now.strftime("%Y%m%d")            # 20260101（用于密钥派生）
        ts = now.strftime("%Y%m%dT%H%M%SZ")      # 20260101T083000Z（X-Date 头）
        self.headers['X-Date'] = ts

        # --- 第 2 步：计算 Body 哈希 ---
        # 【极重要】这里 json.dumps(body) 的结果必须和最终发出的 HTTP Body
        # 字节完全一致！所以 main.py / rag_service.py 发送时要用
        # content=json.dumps(body).encode()，而不能用 httpx 的 json= 参数
        body_str = json.dumps(self.body) if self.body else ''
        body_hash = hashlib.sha256(body_str.encode('utf-8')).hexdigest()
        self.headers['X-Content-Sha256'] = body_hash   # Body 哈希也放进头里参与签名

        # --- 第 3 步：组装规范请求 CanonicalRequest ---
        # 只签名这 4 个头（小写、按字母排序），其他头不参与签名
        signed_headers = sorted([k.lower() for k in self.headers.keys()
                                 if k.lower() in ['content-type', 'host',
                                                  'x-content-sha256', 'x-date']])
        # 规范化头：每行 "key:value\n"（值去首尾空格）
        canonical_headers = "".join(
            [f"{k}:{self.headers.get(key_map(k, self.headers)).strip()}\n"
             for k in signed_headers])
        signed_headers_str = ";".join(signed_headers)   # "content-type;host;x-..."

        # 规范化查询串：参数按 key 排序后 k=v 用 & 连接
        # （本项目参数只有 Action 和 Version，无需 URL 编码）
        query_str = "&".join([f"{k}={v}" for k, v in sorted(self.params.items())])

        # 规范请求格式：方法\n路径\n查询串\n规范化头\n参与签名的头\nBody哈希
        canonical_request = (f"{self.method}\n{self.path}\n{query_str}\n"
                             f"{canonical_headers}\n{signed_headers_str}\n{body_hash}")

        # --- 第 4 步：构建待签名字符串 StringToSign ---
        # 凭证范围：日期/区域/服务名/request
        credential_scope = f"{date}/{self.region}/{self.service}/request"
        string_to_sign = (f"HMAC-SHA256\n{ts}\n{credential_scope}\n"
                          f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}")

        # --- 第 5 步：逐层派生签名密钥（SK 不直接用，派生链更安全） ---
        k_date = hmac_sha256(sk.encode('utf-8'), date)        # SK + 日期
        k_region = hmac_sha256(k_date, self.region)           # + 区域
        k_service = hmac_sha256(k_region, self.service)       # + 服务名
        k_signing = hmac_sha256(k_service, "request")         # + 固定串 "request"

        # --- 第 6 步：用派生密钥对 StringToSign 计算最终签名 ---
        signature = hmac.new(k_signing, string_to_sign.encode('utf-8'),
                             hashlib.sha256).hexdigest()

        # --- 第 7 步：拼装 Authorization 头，塞回 headers ---
        auth_header = (f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
                       f"SignedHeaders={signed_headers_str}, Signature={signature}")
        self.headers['Authorization'] = auth_header


def hmac_sha256(key, msg):
    """HMAC-SHA256 工具函数：返回 bytes 摘要"""
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def key_map(lower_key, headers):
    """根据小写 header 名找到原始大小写的 key（HTTP 头大小写不敏感）"""
    for k in headers.keys():
        if k.lower() == lower_key:
            return k
    return lower_key


def read_files(directory, suffix='.json'):
    """读取目录下所有指定后缀的 JSON 文件，返回 {文件名: 内容} 字典"""
    scenes = {}
    abs_dir = os.path.join(os.path.dirname(__file__), directory)
    if not os.path.exists(abs_dir):
        return scenes

    for filename in os.listdir(abs_dir):
        if filename.endswith(suffix):
            filepath = os.path.join(abs_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    key = filename.replace(suffix, '')   # 去掉后缀作为 key
                    scenes[key] = data
            except Exception as e:
                print(f"Error reading {filename}: {e}")
    return scenes


# 统一成功/失败响应封装：
# 成功 → { "ResponseMetadata": {...}, "Result": 业务数据 }
# 失败 → { "ResponseMetadata": { "Action": ..., "Error": {...} } }
async def response_wrapper(api_name, logic_func, contain_metadata=True):
    response_metadata = {"Action": api_name}
    try:
        res = await logic_func()
        if contain_metadata:
            return {"ResponseMetadata": response_metadata, "Result": res}
        return res
    except Exception as e:
        # 红色打印错误（\x1b[31m 是终端红色色码）
        print(f"\x1b[31mError in {api_name}: {e}\x1b[0m")
        response_metadata["Error"] = {"Code": -1, "Message": str(e)}
        return JSONResponse(content={"ResponseMetadata": response_metadata})


def assert_val(expression, msg):
    """断言工具：值为假、或是含空格的字符串就抛异常（常用于校验 ID 格式）"""
    if not expression or (isinstance(expression, str) and ' ' in expression):
        print(f"\x1b[31m校验失败: {msg}\x1b[0m")
        raise ValueError(msg)

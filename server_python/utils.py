# Server/utils.py
import os
import json
import hashlib
import hmac
import datetime
from fastapi.responses import JSONResponse

# --- 签名工具类 (替代 @volcengine/openapi) ---
class Signer:
    def __init__(self, request_data, service, region='cn-north-1'):
        self.method = request_data.get('method', 'POST').upper()
        self.path = request_data.get('path', '/')
        self.params = request_data.get('params', {})
        self.headers = request_data.get('headers', {})
        self.body = request_data.get('body', {})
        self.service = service
        self.region = region

    def add_authorization(self, account_config):
        ak = account_config.get('accessKeyId')
        sk = account_config.get('secretKey')
        if not ak or not sk:
            return

        # 1. 准备时间
        now = datetime.datetime.utcnow()
        date = now.strftime("%Y%m%d")
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        self.headers['X-Date'] = ts
        
        # 2. 计算 Body Hash (必须与实际发送的字节一致, 使用紧凑格式)
        body_str = json.dumps(self.body, separators=(',', ':')) if self.body else ''
        body_hash = hashlib.sha256(body_str.encode('utf-8')).hexdigest()
        self.headers['X-Content-Sha256'] = body_hash

        # 3. 规范化请求 (CanonicalRequest)
        signed_headers = sorted([k.lower() for k in self.headers.keys() if k.lower() in ['content-type', 'host', 'x-content-sha256', 'x-date']])
        canonical_headers = "".join([f"{k}:{self.headers.get(key_map(k, self.headers)).strip()}\n" for k in signed_headers])
        signed_headers_str = ";".join(signed_headers)
        
        # 简单处理 query，这里假设 params 只有 Action 和 Version
        query_str = "&".join([f"{k}={v}" for k, v in sorted(self.params.items())])

        canonical_request = f"{self.method}\n{self.path}\n{query_str}\n{canonical_headers}\n{signed_headers_str}\n{body_hash}"

        # 4. StringToSign
        credential_scope = f"{date}/{self.region}/{self.service}/request"
        string_to_sign = f"HMAC-SHA256\n{ts}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

        # 5. 计算签名 Key
        k_date = hmac_sha256(sk.encode('utf-8'), date)
        k_region = hmac_sha256(k_date, self.region)
        k_service = hmac_sha256(k_region, self.service)
        k_signing = hmac_sha256(k_service, "request")

        # 6. 计算最终签名
        signature = hmac.new(k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        # 7. 构造 Authorization 头
        auth_header = f"HMAC-SHA256 Credential={ak}/{credential_scope}, SignedHeaders={signed_headers_str}, Signature={signature}"
        self.headers['Authorization'] = auth_header

def hmac_sha256(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

def key_map(lower_key, headers):
    """找到原始 header key"""
    for k in headers.keys():
        if k.lower() == lower_key:
            return k
    return lower_key

# --- 业务工具函数 ---

def read_files(directory, suffix='.json'):
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
                    key = filename.replace(suffix, '')
                    scenes[key] = data
            except Exception as e:
                print(f"Error reading {filename}: {e}")
    return scenes


# 成功 {
#     "ResponseMetadata": { "Action": "getScenes" },
#     "Result": { "scenes": [...] } # logic_func 返回的实际数据
# }

# 失败 {
#     "ResponseMetadata": {
#         "Action": "getScenes",
#         "Error": {
#             "Code": -1,
#             "Message": "Custom 不存在, 请先在 Server/scenes 下定义该场景的 JSON." # 异常的具体描述
#         }
#     }
# }

# 统一响应封装 (替代原 wrapper)
async def response_wrapper(api_name, logic_func, contain_metadata=True):
    response_metadata = {"Action": api_name}
    try:
        res = await logic_func()
        if contain_metadata:
            return {"ResponseMetadata": response_metadata, "Result": res}
        return res
    except Exception as e:
        print(f"\x1b[31mError in {api_name}: {e}\x1b[0m")
        response_metadata["Error"] = {
            "Code": -1,
            "Message": str(e)
        }
        return JSONResponse(content={"ResponseMetadata": response_metadata})

def assert_val(expression, msg):
    # 仅校验"不能为空", 原先的空格检查会误判合法值
    if not expression:
        print(f"\x1b[31m校验失败: {msg}\x1b[0m")
        raise ValueError(msg)
# -*- coding: utf-8 -*-
"""
RTC Token（进房门票）生成器。
前端进 RTC 房间必须携带 Token，服务端用 AppKey 验证身份和权限。

Token 本质是一个"签名的二进制包"，最终字符串结构：
  "001" + AppId + Base64( 消息体二进制 + HMAC-SHA256签名 )
  └版本号┘         └──────ByteBuf 按小端字节序打包──────┘
"""
import time
import struct       # 二进制打包（把数字转成固定长度字节）
import hmac
import hashlib
import base64
import random
from io import BytesIO

VERSION = "001"     # Token 协议版本号，火山固定要求 "001"

# 权限位定义（每种权限对应消息体权限表里的一个 key）
PRIVILEGES = {
    "PrivPublishStream": 0,        # 发布音视频流（推流：自己说话）
    "privPublishAudioStream": 1,   # 发布音频
    "privPublishVideoStream": 2,   # 发布视频
    "privPublishDataStream": 3,    # 发布数据消息
    "PrivSubscribeStream": 4,      # 订阅音视频流（拉流：听 AI 说话）
}


class ByteBuf:
    """二进制打包缓冲区：全部小端字节序（'<H'=2字节、'<I'=4字节）"""

    def __init__(self, data=None):
        self.buffer = BytesIO(data) if data else BytesIO()

    def pack(self):
        """取出打包完成的全部字节"""
        return self.buffer.getvalue()

    def put_uint16(self, v):   # 写入 2 字节无符号整数
        self.buffer.write(struct.pack('<H', v)); return self

    def put_uint32(self, v):   # 写入 4 字节无符号整数
        self.buffer.write(struct.pack('<I', v)); return self

    def put_bytes(self, b):
        """写入字节串：先写 2 字节长度，再写内容（长度前置协议）"""
        self.put_uint16(len(b)); self.buffer.write(b); return self

    def put_string(self, s):
        """写入字符串：UTF-8 编码后按字节串写入"""
        return self.put_bytes(s.encode('utf-8'))

    def put_tree_map_uint32(self, m):
        """写入权限表：先写条数，再逐条写 权限key(2字节) + 过期时间(4字节)"""
        if not m:
            self.put_uint16(0); return self   # 空表只写条数 0
        self.put_uint16(len(m))
        for k, v in m.items():
            self.put_uint16(int(k)); self.put_uint32(int(v))
        return self


class AccessToken:
    """RTC Token 构造器"""

    def __init__(self, app_id, app_key, room_id, user_id):
        self.app_id = app_id          # RTC 应用 ID
        self.app_key = app_key        # RTC 应用密钥（签名用，绝不外传）
        self.room_id = room_id        # 允许进入的房间
        self.user_id = user_id        # 允许使用的身份
        self.issued_at = int(time.time())               # 签发时间
        self.nonce = random.randint(0, 0xFFFFFFFF)      # 随机数（防重放）
        self.expire_at = 0                              # 过期时间
        self.privileges = {}                            # 权限表

    def add_privilege(self, privilege, expire_timestamp):
        """
        添加权限。expire_timestamp=0 表示权限永不过期（跟随 Token 整体过期）。
        特别地：授予"发布流"权限时，自动连带授予音频/视频/数据三个子权限
        （火山协议要求，否则可能出现能推流但音/视频单独校验失败）
        """
        self.privileges[privilege] = expire_timestamp
        if privilege == PRIVILEGES["PrivPublishStream"]:
            self.privileges[PRIVILEGES["privPublishVideoStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishAudioStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishDataStream"]] = expire_timestamp

    def expire_time(self, expire_timestamp):
        """设置 Token 整体过期时间（Unix 秒级时间戳）"""
        self.expire_at = expire_timestamp

    def pack_msg(self):
        """打包消息体（未签名的原始二进制）：
        nonce + issuedAt + expireAt + roomId + userId + 权限表"""
        buf = ByteBuf()
        buf.put_uint32(self.nonce)
        buf.put_uint32(self.issued_at)
        buf.put_uint32(self.expire_at)
        buf.put_string(self.room_id)
        buf.put_string(self.user_id)
        buf.put_tree_map_uint32(self.privileges)
        return buf.pack()

    def serialize(self):
        """
        最终序列化：消息体 + HMAC-SHA256 签名一起 Base64，
        前拼版本号 "001" 和 AppId（明文部分，服务端据此找到对应 AppKey 验签）
        """
        msg = self.pack_msg()
        # 用 AppKey 对消息体做 HMAC 签名 —— 服务端用同一个 AppKey 重算比对
        signature = hmac.new(self.app_key.encode('utf-8'), msg,
                             hashlib.sha256).digest()
        # 拼接：消息体 + 签名（各带 2 字节长度前缀）
        content = ByteBuf().put_bytes(msg).put_bytes(signature).pack()
        return VERSION + self.app_id + base64.b64encode(content).decode('utf-8')

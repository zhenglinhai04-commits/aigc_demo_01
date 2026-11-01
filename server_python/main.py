# Server/main.py
import uuid
import json
import time
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from token_builder import AccessToken, PRIVILEGES
from utils import read_files, assert_val, response_wrapper, Signer

app = FastAPI()

# 允许跨域 (替代 koa2-cors)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 读取场景配置
SCENES = read_files('./scenes', '.json')

@app.post("/proxy")
async def proxy(request: Request):
    """
    代理 AIGC 的 OpenAPI 请求
    """
    # 获取 query 参数

    
    action = request.query_params.get("Action")
    version = request.query_params.get("Version", "2025-06-01")
    
    # 获取 body
    try:
        body_data = await request.json()
    except:
        body_data = {}
    # 发送请求
    async def logic():
        assert_val(action, 'Action 不能为空')
        assert_val(version, 'Version 不能为空')

        scene_id = body_data.get("SceneID")
        assert_val(scene_id, 'SceneID 不能为空, SceneID 用于指定场景的 JSON')

        json_data = SCENES.get(scene_id)
        assert_val(json_data, f"{scene_id} 不存在, 请先在 Server/scenes 下定义该场景的 JSON.")

        voice_chat = json_data.get("VoiceChat", {})
        account_config = json_data.get("AccountConfig", {})
        
        assert_val(account_config.get("accessKeyId"), 'AccountConfig.accessKeyId 不能为空')
        assert_val(account_config.get("secretKey"), 'AccountConfig.secretKey 不能为空')

        request_body = {}
        if action == 'StartVoiceChat':
            request_body = voice_chat
        elif action == 'StopVoiceChat':
            app_id = voice_chat.get("AppId")
            room_id = voice_chat.get("RoomId")
            task_id = voice_chat.get("TaskId")
            
            assert_val(app_id, 'VoiceChat.AppId 不能为空')
            assert_val(room_id, 'VoiceChat.RoomId 不能为空')
            assert_val(task_id, 'VoiceChat.TaskId 不能为空')
            
            request_body = {
                "AppId": app_id,
                "RoomId": room_id,
                "TaskId": task_id
            }

        # 构造并签名请求
        host = "rtc.volcengineapi.com"
        open_api_request_data = {
            "method": "POST",
            "path": "/",
            "params": {"Action": action, "Version": version},
            "headers": {
                "Host": host,
                "Content-Type": "application/json"
            },
            "body": request_body
        }

        signer = Signer(open_api_request_data, "rtc")
        signer.add_authorization(account_config)

        # 发起真实请求 (body 字节必须与签名时的完全一致)
        url = f"https://{host}?Action={action}&Version={version}"
        body_bytes = json.dumps(request_body, separators=(',', ':')).encode('utf-8') if request_body else b''
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, 
                headers=open_api_request_data['headers'], 
                content=body_bytes,
                timeout=30.0
            )
            return resp.json()

    return await response_wrapper('proxy', logic, contain_metadata=False)

# 返回值示意
# {
#   "ResponseMetadata": {
#     "Action": "getScenes"
#   },
#   "Result": {
#     "scenes": [
#       {
#         "scene": {
#           "id": "Custom", 
#           "botName": "vc_user_12345",
#           "isInterruptMode": true,
#           "isVision": false,
#           "isScreenMode": false,
#           "isAvatarScene": true,
#           "avatarBgUrl": "https://example.com/bg.jpg"
#         },
#         "rtc": {
#           "AppId": "your_rtc_app_id",
#           "RoomId": "550e8400-e29b-41d4-a716-446655440000",
#           "UserId": "63f10842-1463-488b-967b-123456789abc",
#           "Token": "001your_rtc_app_idBASE64_ENCODED_TOKEN_STRING..."
#         }
#       }
#     ]
#   }
# }

# 字段详细说明：
# scene (前端 UI 配置):

# id: 对应 Server/scenes/ 目录下 JSON 文件的文件名（如 Custom）。

# botName: 智能体的 ID（从 VoiceChat.AgentConfig.UserId 获取）。

# isInterruptMode: 是否支持打断（InterruptMode == 0 为支持）。

# isVision: 是否开启视觉/多模态功能。

# isAvatarScene: 是否是数字人场景。

# rtc (加入房间参数):

# AppId: 火山引擎 RTC 应用 ID。

# RoomId: 房间 ID。如果你在 JSON 里没写，程序会自动生成一个 UUID。

# UserId: 用户的唯一 ID。同上，没写则自动生成。

# Token: 最重要的部分。这是后台利用 AppKey 算出来的鉴权字符串，前端拿到后才能进入 RTC 房间。


@app.post("/getScenes")
async def get_scenes(request: Request):
    """
    获取场景列表并自动生成 Token
    """
    async def logic():
        result_scenes = []
        for key, data in SCENES.items():
            scene_config = data.get("SceneConfig", {})
            rtc_config = data.get("RTCConfig", {})
            voice_chat = data.get("VoiceChat", {})
            
            app_id = rtc_config.get("AppId")
            room_id = rtc_config.get("RoomId")
            user_id = rtc_config.get("UserId")
            token = rtc_config.get("Token")
            app_key = rtc_config.get("AppKey")

            assert_val(app_id, f"{key} 场景的 RTCConfig.AppId 不能为空")

            # 对齐 Node 原版逻辑: 自动补齐 VoiceChat.AppId 和 TaskId (StartVoiceChat 必填)
            if app_id:
                voice_chat["AppId"] = voice_chat.get("AppId") or app_id
                voice_chat["TaskId"] = voice_chat.get("TaskId") or str(uuid.uuid4())

            # 自动生成 Token 逻辑
            if app_id and (not token or not user_id or not room_id):
                # 如果没有配置，自动生成并回填
                new_room_id = room_id or str(uuid.uuid4())
                new_user_id = user_id or str(uuid.uuid4())
                
                rtc_config["RoomId"] = new_room_id
                # 注意：Node 代码中是 VoiceChat.RoomId = ...，此处同步修改
                voice_chat["RoomId"] = new_room_id
                
                rtc_config["UserId"] = new_user_id
                # 同步修改 VoiceChat.AgentConfig.TargetUserId[0]
                if voice_chat.get("AgentConfig") and isinstance(voice_chat["AgentConfig"].get("TargetUserId"), list):
                    voice_chat["AgentConfig"]["TargetUserId"][0] = new_user_id

                assert_val(app_key, f"自动生成 Token 时, {key} 场景的 AppKey 不可为空")
                
                # 生成 Token
                token_builder = AccessToken(app_id, app_key, new_room_id, new_user_id)
                token_builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
                token_builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
                token_builder.expire_time(int(time.time()) + (24 * 3600))
                
                rtc_config["Token"] = token_builder.serialize()

            # 构造前端所需的 SceneConfig
            scene_config["id"] = key
            scene_config["botName"] = voice_chat.get("AgentConfig", {}).get("UserId")
            
            interrupt_mode = voice_chat.get("Config", {}).get("InterruptMode")
            scene_config["isInterruptMode"] = (interrupt_mode == 0)

            llm_config = voice_chat.get("Config", {}).get("LLMConfig", {})
            vision_config = llm_config.get("VisionConfig", {})
            scene_config["isVision"] = vision_config.get("Enable")

            snapshot_config = vision_config.get("SnapshotConfig", {})
            scene_config["isScreenMode"] = (snapshot_config.get("StreamType") == 1)

            avatar_config = voice_chat.get("Config", {}).get("AvatarConfig", {})
            scene_config["isAvatarScene"] = avatar_config.get("Enabled")
            scene_config["avatarBgUrl"] = avatar_config.get("BackgroundUrl")

            # 移除敏感的 AppKey
            rtc_config_safe = rtc_config.copy()
            if "AppKey" in rtc_config_safe:
                del rtc_config_safe["AppKey"]

            result_scenes.append({
                "scene": scene_config,
                "rtc": rtc_config_safe
            })
            
        return {"scenes": result_scenes}

    return await response_wrapper('getScenes', logic)

if __name__ == "__main__":
    print("AIGC Server is running at http://0.0.0.0:3001")
    # 启用 reload 模式，监听文件变动 (类似 nodemon)
    uvicorn.run("main:app", host="0.0.0.0", port=3001, reload=True)
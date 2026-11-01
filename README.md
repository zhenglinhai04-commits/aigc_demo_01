# Node Server

## 启动命令
```
yarn

yarn dev
```

## 使用须知
Node 服务启动时会自动读取 `Server/scenes` 下的所有文件作为可用的场景, 并通过接口 API 返回相关信息。

因此，您需要：
1. 在 `Server/scenes` 目录下参考其它 JSON 的格式, 自定义创建一个 `xxxx.json` 文件，用于描述您的场景，其中 xxxx 为场景名称。
2. 确保您的 `.json` 文件符合模版定义(可参考 Custom.json), 大小写敏感。
3. 新增场景 JSON 后须重启 Node 服务，保证场景信息被正常读取。
4. JSON 文件中, 若 `RTCConfig.RoomId`、`RTCConfig.UserId`、`RTCConfig.Token` 其中之一未填写, Node 服务将自动生成对应的值以保证对话可以正常启动。


## 相关参数获取
- AccountConfig
    - 可在 https://console.volcengine.com/iam/keymanage/ 获取 AK/SK。
- RTCConfig
    - AppId、AppKey 可从 https://console.volcengine.com/rtc/aigc/listRTC 中获取。
    - RoomId、UserId 可自定义也可不填，交由服务端生成。
- VoiceChat
    - 可参考 https://www.volcengine.com/docs/6348/1558163 中参数描述
    - 可通过 [快速跑通 Demo](https://console.volcengine.com/rtc/aigc/run?s=g) 快速获取参数, 跑通后点击右上角 `接入 API` 按钮复制相关代码贴到 JSON 配置文件中即可。


## 注意
- 相关错误会通过服务端接口返回。
- Node 服务会根据您配置的 `VoiceChat` 中是否存在视觉模型相关的配置返回相关信息给前端页面, 从而控制相关 UI 是否展示。
- 使用时请留意相关服务已开通。
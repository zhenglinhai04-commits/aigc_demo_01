/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import VERTC, {
  LocalAudioPropertiesInfo,
  RemoteAudioPropertiesInfo,
  LocalStreamStats,
  MediaType,
  onUserJoinedEvent,
  onUserLeaveEvent,
  RemoteStreamStats,
  StreamRemoveReason,
  StreamIndex,
  DeviceInfo,
  AutoPlayFailedEvent,
  PlayerEvent,
  NetworkQuality,
} from '@volcengine/rtc';
import { useDispatch } from 'react-redux';
import { useRef } from 'react';

import {
  IUser,
  remoteUserJoin,
  remoteUserLeave,
  updateLocalUser,
  updateRemoteUser,
  addAutoPlayFail,
  removeAutoPlayFail,
  updateNetworkQuality,
} from '@/store/slices/room';
import RtcClient, { IEventListener } from './RtcClient';

import { setMicrophoneList, updateSelectedDevice } from '@/store/slices/device';
import { useMessageHandler } from '@/utils/handler';
import store from '@/store';

/**
 * 自定义 Hook：管理所有的 RTC 事件监听
 * 将 RTC SDK 的底层事件映射为 Redux 状态更新
 */
const useRtcListeners = (): IEventListener => {
  const dispatch = useDispatch();
  const { parser } = useMessageHandler(); // 从语义处理器中获取解析逻辑

  // 内部引用：持久化记录房间内各用户的播放状态，不触发渲染
  const playStatus = useRef<{ [key: string]: { audio: boolean; video: boolean } }>({});

  /**
   * 轨道结束回调：主要用于处理浏览器原生“停止共享”按钮被点击后的逻辑同步
   */
  const handleTrackEnded = async (event: { kind: string; isScreen: boolean }) => {
    const { kind, isScreen } = event;
    if (isScreen && kind === 'video') {
      await RtcClient.stopScreenCapture();
      await RtcClient.unpublishScreenStream(MediaType.VIDEO);
      dispatch(updateLocalUser({ publishScreen: false })); // 更新 UI 状态
    }
  };

  /**
   * 用户加入房间回调：解析 extraInfo 获取业务层面的用户名
   */
  const handleUserJoin = (e: onUserJoinedEvent) => {
    const extraInfo = JSON.parse(e.userInfo.extraInfo || '{}');
    const userId = extraInfo.user_id || e.userInfo.userId;
    const username = extraInfo.user_name || e.userInfo.userId;
    dispatch(remoteUserJoin({ userId, username }));
  };

  /**
   * 错误处理：处理相同 ID 重复登录等异常
   */
  const handleError = (e: { errorCode: typeof VERTC.ErrorCode.DUPLICATE_LOGIN }) => {
    const { errorCode } = e;
    if (errorCode === VERTC.ErrorCode.DUPLICATE_LOGIN) {
      console.log('踢人'); // 可以在此处理被迫下线的 UI 提示
    }
  };

  /**
   * 用户离开房间回调
   */
  const handleUserLeave = (e: onUserLeaveEvent) => {
    dispatch(remoteUserLeave(e.userInfo));
    dispatch(removeAutoPlayFail(e.userInfo)); // 清理该用户的自动播放失败标记
  };

  /**
   * 远端用户发布流回调（最核心）：当 AI 说话或开启视频时触发
   */
  const handleUserPublishStream = (e: { userId: string; mediaType: MediaType }) => {
    const { userId, mediaType } = e;
    const payload: IUser = { userId };
    
    // 标记媒体发布状态
    if (mediaType === MediaType.AUDIO) {
      payload.publishAudio = true;
    } else if (mediaType === MediaType.VIDEO) {
      payload.publishVideo = true;
    } else if (mediaType === MediaType.AUDIO_AND_VIDEO) {
      payload.publishAudio = true;
      payload.publishVideo = true;
    }

    // 根据全屏状态动态指定视频渲染的 DOM 容器 ID
    const isFullScreen = store.getState().room.isFullScreen;
    RtcClient.setRemoteVideoPlayer(userId, isFullScreen ? 'remote-video-player' : 'remote-full-player');
    console.log('handleUserPublishStream Ai开始说话了', userId, mediaType);
    dispatch(updateRemoteUser(payload));
  };

  /**
   * 远端用户取消发布流回调：清理视频视图并更新状态
   */
  const handleUserUnpublishStream = (e: {
    userId: string;
    mediaType: MediaType;
    reason: StreamRemoveReason;
  }) => {
    const { userId, mediaType } = e;
    const payload: IUser = { userId };

    if (mediaType === MediaType.AUDIO) {
      payload.publishAudio = false;
    }
    if (mediaType === MediaType.AUDIO_AND_VIDEO) {
      payload.publishAudio = false;
    }

    RtcClient.setRemoteVideoPlayer(userId); // 传入空容器 ID 即为解除绑定
    dispatch(updateRemoteUser(payload));
  };

  /**
   * 远端流统计信息：用于显示网络质量、丢包率等技术参数
   */
  const handleRemoteStreamStats = (e: RemoteStreamStats) => {
    dispatch(updateRemoteUser({ userId: e.userId, audioStats: e.audioStats }));
  };

  /**
   * 本地流统计信息
   */
  const handleLocalStreamStats = (e: LocalStreamStats) => {
    dispatch(updateLocalUser({ audioStats: e.audioStats }));
  };

  /**
   * 本地音量报告：用于绘制用户自己的音波图
   */
  const handleLocalAudioPropertiesReport = (e: LocalAudioPropertiesInfo[]) => {
    const localAudioInfo = e.find(
      (audioInfo) => audioInfo.streamIndex === StreamIndex.STREAM_INDEX_MAIN
    );
    if (localAudioInfo) {
      dispatch(updateLocalUser({ audioPropertiesInfo: localAudioInfo.audioPropertiesInfo }));
    }
  };

  /**
   * 远端音量报告：用于绘制 AI 或其他用户的音波图
   */
  const handleRemoteAudioPropertiesReport = (e: RemoteAudioPropertiesInfo[]) => {
    const remoteAudioInfo = e
      .filter((audioInfo) => audioInfo.streamKey.streamIndex === StreamIndex.STREAM_INDEX_MAIN)
      .map((audioInfo) => ({
        userId: audioInfo.streamKey.userId,
        audioPropertiesInfo: audioInfo.audioPropertiesInfo,
      }));

    if (remoteAudioInfo.length) {
      dispatch(updateRemoteUser(remoteAudioInfo));
    }
  };

  /**
   * 硬件设备状态变化：处理麦克风插拔、耳机断连等热切换逻辑
   */
  const handleAudioDeviceStateChanged = async (device: DeviceInfo) => {
    const devices = await RtcClient.getDevices();

    if (device.mediaDeviceInfo.kind === 'audioinput') {
      let deviceId = device.mediaDeviceInfo.deviceId;
      // 如果当前使用的设备失效，自动切换到列表中第一个可用设备
      if (device.deviceState === 'inactive') {
        deviceId = devices.audioInputs?.[0].deviceId || '';
      }
      RtcClient.switchDevice(MediaType.AUDIO, deviceId);
      dispatch(setMicrophoneList(devices.audioInputs));
      dispatch(updateSelectedDevice({ selectedMicrophone: deviceId }));
    }
  };

  /**
   * 浏览器自动播放失败回调：捕获并提示用户通过交互激活声音
   */
  const handleAutoPlayFail = (event: AutoPlayFailedEvent) => {
    const { userId, kind } = event;
    let playUser = playStatus.current?.[userId] || {};
    playUser = { ...playUser, [kind]: false };
    playStatus.current[userId] = playUser;

    dispatch(addAutoPlayFail({ userId }));
  };

  const addFailUser = (userId: string) => {
    dispatch(addAutoPlayFail({ userId }));
  };

  /**
   * 播放失败内部逻辑：记录失败的媒体类型（音/视）
   */
  const playerFail = (params: { type: 'audio' | 'video'; userId: string }) => {
    const { type, userId } = params;
    let playUser = playStatus.current?.[userId] || {};
    playUser = { ...playUser, [type]: false };
    const { audio, video } = playUser;
    if (audio === false || video === false) {
      addFailUser(userId);
    }
    return playUser;
  };

  /**
   * 播放状态事件监听：处理 HTML5 Video/Audio 标签的 playing 或 pause 事件
   */
  const handlePlayerEvent = (event: PlayerEvent) => {
    const { userId, rawEvent, type } = event;
    let playUser = playStatus.current?.[userId] || {};

    if (!playStatus.current) return;

    if (rawEvent.type === 'playing') {
      playUser = { ...playUser, [type]: true };
      const { audio, video } = playUser;
      // 如果音视频都正常播放了，移除 UI 上的播放失败警告
      if (audio !== false && video !== false) {
        dispatch(removeAutoPlayFail({ userId }));
      }
    } else if (rawEvent.type === 'pause') {
      playUser = playerFail({ type, userId });
    }

    playStatus.current[userId] = playUser;
  };

  /**
   * 网络质量回调：取上行和下行的平均值更新 UI 信号图标
   */
  const handleNetworkQuality = (
    uplinkNetworkQuality: NetworkQuality,
    downlinkNetworkQuality: NetworkQuality
  ) => {
    dispatch(
      updateNetworkQuality({
        networkQuality: Math.floor(
          (uplinkNetworkQuality + downlinkNetworkQuality) / 2
        ) as NetworkQuality,
      })
    );
  };

  /**
   * 接收房间二进制消息回调：Agent 发送的语义数据（字幕、状态）通过此处进入解析器
   */
  const handleRoomBinaryMessageReceived = (event: { userId: string; message: ArrayBuffer }) => {
    const { message } = event;
    parser(message); // 调用 handler.ts 中的语义解析逻辑
  };

  // 返回所有的处理函数给 RtcClient 绑定
  return {
    handleError,
    handleUserJoin,
    handleUserLeave,
    handleTrackEnded,
    handleUserPublishStream,
    handleUserUnpublishStream,
    handleRemoteStreamStats,
    handleLocalStreamStats,
    handleLocalAudioPropertiesReport,
    handleRemoteAudioPropertiesReport,
    handleAudioDeviceStateChanged,
    handleAutoPlayFail,
    handlePlayerEvent,
    handleRoomBinaryMessageReceived,
    handleNetworkQuality,
  };
};

export default useRtcListeners;
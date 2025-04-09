"""XiaoZhi ESP32 voice assistant integration."""
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.components import assist_pipeline
from homeassistant.helpers.network import get_url

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_WEBSOCKET_PORT,
    CONF_WEBSOCKET_PATH,
    CONF_PIPELINE_ID,
    CONF_FORWARD_URL,
    SERVICE_SEND_TTS,
    SERVICE_GET_DEVICE_CONFIG,
    ATTR_DEVICE_ID,
    ATTR_MESSAGE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_FALLBACK_URL,
)
from .websocket_server import XiaozhiWebSocket

_LOGGER = logging.getLogger(__name__)

SEND_TTS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_MESSAGE): cv.string,
    }
)

GET_DEVICE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_FALLBACK_URL): cv.string,
    }
)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the XiaoZhi ESP32 component."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up XiaoZhi ESP32 from a config entry."""
    _LOGGER.info("Setting up XiaoZhi ESP32 assistant")
    
    # 存储配置
    config = entry.data
    
    # 获取配置项
    pipeline_id = config.get(CONF_PIPELINE_ID)
    websocket_port = config.get(CONF_WEBSOCKET_PORT)
    websocket_path = config.get(CONF_WEBSOCKET_PATH)
    forward_url = config.get(CONF_FORWARD_URL)
    
    # 检查语音助手Pipeline是否存在
    pipelines = await assist_pipeline.async_get_pipelines(hass)
    pipeline_exists = any(p.id == pipeline_id for p in pipelines)
    
    if not pipeline_exists:
        _LOGGER.error("指定的语音助手Pipeline不存在: %s", pipeline_id)
        return False
    
    # 初始化WebSocket服务
    websocket = XiaozhiWebSocket(
        hass=hass, 
        port=websocket_port, 
        websocket_path=websocket_path,
        pipeline_id=pipeline_id,
        forward_url=forward_url
    )
    
    await websocket.start()
    
    # 存储WebSocket实例以便后续使用
    hass.data[DOMAIN][entry.entry_id] = {
        "websocket": websocket,
    }
    
    _LOGGER.info("XiaoZhi ESP32助手WebSocket服务已启动，监听端口 %s，路径 %s", 
                websocket_port, websocket_path)
    
    # 注册服务
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_TTS,
        _async_send_tts,
        schema=SEND_TTS_SCHEMA,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DEVICE_CONFIG,
        _get_device_config,
        schema=GET_DEVICE_CONFIG_SCHEMA,
    )
    
    # 设置平台
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # 卸载平台
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # 停止WebSocket服务
    if entry.entry_id in hass.data[DOMAIN]:
        websocket = hass.data[DOMAIN][entry.entry_id].get("websocket")
        if websocket:
            await websocket.stop()
        
        # 移除服务和数据
        hass.services.async_remove(DOMAIN, SERVICE_SEND_TTS)
        hass.services.async_remove(DOMAIN, SERVICE_GET_DEVICE_CONFIG)
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok

async def _async_send_tts(hass: HomeAssistant, service_call: ServiceCall) -> None:
    """发送TTS消息到设备服务。"""
    device_id = service_call.data.get(ATTR_DEVICE_ID)
    message = service_call.data.get(ATTR_MESSAGE)
    
    if not device_id or not message:
        _LOGGER.error("发送TTS服务调用缺少必要参数")
        return
    
    # 查找WebSocket服务实例
    websocket = None
    for entry_data in hass.data[DOMAIN].values():
        if "websocket" in entry_data:
            websocket = entry_data["websocket"]
            break
    
    if not websocket:
        _LOGGER.error("无法找到WebSocket服务实例")
        return
    
    # 发送TTS消息
    await websocket.send_tts_message(device_id, message)

async def _get_device_config(hass: HomeAssistant, service_call: ServiceCall) -> None:
    """生成设备配置信息。"""
    config_entry_id = service_call.data.get(ATTR_CONFIG_ENTRY_ID)
    device_id = service_call.data.get(ATTR_DEVICE_ID, "xiaozhi_device")
    fallback_url = service_call.data.get(ATTR_FALLBACK_URL)
    
    if config_entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("找不到指定的配置条目ID: %s", config_entry_id)
        return
    
    entry_data = hass.data[DOMAIN][config_entry_id]
    websocket = entry_data.get("websocket")
    
    if not websocket:
        _LOGGER.error("无法找到WebSocket服务实例")
        return
    
    # 获取Home Assistant外部URL
    try:
        external_url = get_url(hass, prefer_external=True)
    except Exception:
        external_url = fallback_url
        
    if not external_url:
        _LOGGER.error("无法获取Home Assistant外部URL，且未提供备选URL")
        return
    
    # 构建WebSocket URL
    ws_path = websocket.websocket_path.lstrip("/")
    websocket_url = f"{external_url.rstrip('/')}/{ws_path}"
    
    # 将http改为ws，https改为wss
    websocket_url = websocket_url.replace("http://", "ws://").replace("https://", "wss://")
    
    # 生成配置信息
    config_info = {
        "device_id": device_id,
        "websocket_url": websocket_url,
        "reconnect_interval": 5000,
        "ping_interval": 30000
    }
    
    # 在日志中显示配置信息，实际应用中应该创建通知或UI显示
    _LOGGER.info("XiaoZhi ESP32设备配置信息:")
    _LOGGER.info("设备ID: %s", config_info["device_id"])
    _LOGGER.info("WebSocket URL: %s", config_info["websocket_url"])
    _LOGGER.info("重连间隔(ms): %s", config_info["reconnect_interval"])
    _LOGGER.info("心跳间隔(ms): %s", config_info["ping_interval"])
    _LOGGER.info("请将此配置信息用于ESP32固件编译") 
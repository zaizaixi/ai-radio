"""AI 电台直播助手。

模块划分：
    config      配置加载（TOML）
    wsclient    纯标准库 WebSocket 客户端
    chat        聊天源：B 站弹幕 / 控制台模拟
    llm         大模型大脑（DeepSeek，OpenAI 兼容）
    tts         语音合成 + 播放
    persona     人设与提示词
    memory      对话记忆 / 稿件去重
    director    电台调度中枢
"""

__version__ = "0.8"
__all__ = ["__version__"]

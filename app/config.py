from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./polly.db"

    openai_api_key: str = ""
    openai_explain_model: str = "gpt-4o"
    openai_classify_model: str = "gpt-4o-mini"
    # 留空 = 默认走 api.openai.com（官方）。
    # 第三方代理（智增增 / OneAPI / aaai.vip 等）填代理的 base_url，如 https://api.aaai.vip/v1
    openai_base_url: str = ""

    host: str = "127.0.0.1"
    port: int = 8000

    # ingest 生成 thumbnail_url / video_url / subtitle_url 时用的对外地址。
    # bind 端口和对外端口可能不同（生产是 Nginx 转发），所以单独配置。
    # 留空则回退到 http://{host}:{port}。
    public_base_url: str = ""

    cors_origins: str = "*"

    # 鉴权：所有 /v1/ai/* 端点必须带 Authorization: Bearer <polly_api_key>。
    # 空字符串 = 关闭鉴权（仅本地纯开发用）。生产必须设。
    polly_api_key: str = ""

    # 限速：每 IP 对 AI 端点的速率上限。语法："60/hour"、"10/minute"、"3/second"。
    rate_limit_ai: str = "60/hour"
    # /v1/contents/latest 单独配，可放宽
    rate_limit_contents: str = "300/hour"

    # 限速后端：空 = 进程内内存（单 worker 用）；redis://host:port/db = 多 worker 共享窗口
    redis_url: str = ""

    # 资源存储后端。false = 本地 cdn-staging/（dev）；true = Cloudflare R2（生产）。
    use_r2: bool = False
    r2_endpoint: str = ""  # https://<account-id>.r2.cloudflarestorage.com
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "polly"
    r2_public_base: str = ""  # https://cdn.polly.app  ← 公网 URL 前缀（custom domain）

    def base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

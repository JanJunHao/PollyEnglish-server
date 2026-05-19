"""资源存储抽象层。

- 开发期：LocalStorage 写到 polly-server/cdn-staging/，FastAPI 通过 /static 暴露。
- 生产期：R2Storage 用 S3 兼容协议上传到 Cloudflare R2，URL 走 r2_public_base（custom domain）。

切换靠 .env 的 USE_R2 开关。ingest.py / pregenerate / 任何写资源的脚本都用 `get_storage()`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class Storage(Protocol):
    """统一的"上传文件并返回公网 URL"接口。"""

    def put(self, local_src: Path, remote_key: str, *, content_type: str | None = None) -> str:
        """把本地文件 put 到远端 key，返回可由 iOS 访问的 URL。"""
        ...

    def url(self, remote_key: str) -> str:
        """已存在的 key 的访问 URL。"""
        ...


class LocalStorage:
    """开发期：把文件拷到 cdn-staging/<key>，URL 走 /static/<key>。"""

    def __init__(self, staging_root: Path, public_base: str) -> None:
        self.staging_root = staging_root
        self.public_base = public_base.rstrip("/")
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def put(self, local_src: Path, remote_key: str, *, content_type: str | None = None) -> str:
        dst = self.staging_root / remote_key
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 跨设备防错 + 替换已有
        dst.write_bytes(local_src.read_bytes())
        return self.url(remote_key)

    def url(self, remote_key: str) -> str:
        return f"{self.public_base}/static/{remote_key.lstrip('/')}"


class R2Storage:
    """Cloudflare R2（S3 兼容）：用 boto3 上传，URL 走 r2_public_base（自定义域名）。"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, public_base: str) -> None:
        try:
            import boto3  # 延迟导入，让 dev 环境不强依赖
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 not installed. Run: pip install -e '.[r2]'"
            ) from exc

        self.bucket = bucket
        self.public_base = public_base.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )

    def put(self, local_src: Path, remote_key: str, *, content_type: str | None = None) -> str:
        extra: dict = {}
        if content_type:
            extra["ContentType"] = content_type
        self.client.upload_file(str(local_src), self.bucket, remote_key, ExtraArgs=extra or None)
        return self.url(remote_key)

    def url(self, remote_key: str) -> str:
        return f"{self.public_base}/{remote_key.lstrip('/')}"


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.use_r2:
        missing = [k for k, v in {
            "R2_ENDPOINT": s.r2_endpoint,
            "R2_ACCESS_KEY_ID": s.r2_access_key_id,
            "R2_SECRET_ACCESS_KEY": s.r2_secret_access_key,
            "R2_PUBLIC_BASE": s.r2_public_base,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"USE_R2=true but missing: {', '.join(missing)}")
        return R2Storage(
            endpoint=s.r2_endpoint,
            access_key=s.r2_access_key_id,
            secret_key=s.r2_secret_access_key,
            bucket=s.r2_bucket,
            public_base=s.r2_public_base,
        )

    # 本地：cdn-staging 路径与 main.py 里 StaticFiles 挂载一致
    staging_root = Path(__file__).resolve().parent.parent / "cdn-staging"
    return LocalStorage(staging_root=staging_root, public_base=s.base_url())


# MIME 推断（小集，覆盖 Polly 当前所有资源类型）
_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".vtt": "text/vtt",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def guess_content_type(path: Path) -> str | None:
    return _CONTENT_TYPES.get(path.suffix.lower())
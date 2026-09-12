import asyncio
import json
import logging
import os
from typing import Any

import firebase_admin
from firebase_admin import auth, credentials, messaging

logger = logging.getLogger(__name__)


class FirebaseRuntime:
    """Owns the Firebase Admin app used by authentication and Cloud Messaging."""

    def __init__(
        self,
        database_url: str | None,
        project_id: str | None = None,
        app_name: str = "songlist-backend",
        service_account_json: str = "",
    ) -> None:
        self.database_url = database_url
        self.project_id = project_id
        self.app_name = app_name
        self._service_account_json = service_account_json
        self.app: firebase_admin.App | None = None
        self._owns_app = False

    @classmethod
    def from_environment(cls) -> "FirebaseRuntime":
        return cls(
            database_url=os.getenv("FIREBASE_DATABASE_URL"),
            project_id=os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
            app_name=os.getenv("FIREBASE_APP_NAME", "songlist-backend"),
            service_account_json=os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", ""),
        )

    @property
    def enabled(self) -> bool:
        return self.app is not None

    def initialize(self) -> bool:
        """Initialize Firebase with a service-account key or Application Default Credentials."""
        try:
            self.app = firebase_admin.get_app(self.app_name)
            return True
        except ValueError:
            pass

        options: dict[str, Any] = {}
        if self.database_url:
            options["databaseURL"] = self.database_url
        if self.project_id:
            options["projectId"] = self.project_id

        cred: credentials.Base | None = None
        if self._service_account_json:
            try:
                sa = json.loads(self._service_account_json)
                cred = credentials.Certificate(sa)
            except Exception as exc:
                logger.warning("firebase: bad service account JSON — %s", exc)

        self.app = firebase_admin.initialize_app(
            credential=cred,
            options=options or None,
            name=self.app_name,
        )
        self._owns_app = True
        return True

    async def verify_id_token(self, token: str) -> dict[str, Any]:
        if not self.app:
            raise RuntimeError("Firebase is not configured")
        return await asyncio.to_thread(
            auth.verify_id_token,
            token,
            self.app,
            check_revoked=True,
        )

    async def disable_and_revoke(self, uid: str) -> None:
        if not self.app:
            raise RuntimeError("Firebase is not configured")
        try:
            await asyncio.to_thread(auth.update_user, uid, disabled=True, app=self.app)
            await asyncio.to_thread(auth.revoke_refresh_tokens, uid, app=self.app)
        except auth.UserNotFoundError:
            pass

    async def creation_time(self, uid: str) -> float:
        record = await asyncio.to_thread(auth.get_user, uid, app=self.app)
        return record.user_metadata.creation_timestamp / 1000

    async def delete_user(self, uid: str) -> None:
        if not self.app:
            raise RuntimeError("Firebase is not configured")
        try:
            await asyncio.to_thread(auth.delete_user, uid, app=self.app)
        except auth.UserNotFoundError:
            pass

    async def send_push_notification(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Send an FCM multicast notification to a list of device tokens. Best-effort."""
        if not self.app or not tokens:
            return
        msg = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            data=data or {},
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default"),
                ),
            ),
        )
        try:
            await asyncio.to_thread(messaging.send_each_for_multicast, msg, app=self.app)
        except Exception as exc:
            logger.warning("fcm: send failed — %s", exc)

    async def close(self) -> None:
        if self.app and self._owns_app:
            await asyncio.to_thread(firebase_admin.delete_app, self.app)
        self.app = None
        self._owns_app = False

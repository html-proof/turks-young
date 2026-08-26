import asyncio
import os
from typing import Any

import firebase_admin
from firebase_admin import auth


class FirebaseRuntime:
    """Owns the Firebase Admin app used by authentication and Realtime Database."""

    def __init__(
        self,
        database_url: str | None,
        project_id: str | None = None,
        app_name: str = "songlist-backend",
    ) -> None:
        self.database_url = database_url
        self.project_id = project_id
        self.app_name = app_name
        self.app: firebase_admin.App | None = None
        self._owns_app = False

    @classmethod
    def from_environment(cls) -> "FirebaseRuntime":
        return cls(
            database_url=os.getenv("FIREBASE_DATABASE_URL"),
            project_id=os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
            app_name=os.getenv("FIREBASE_APP_NAME", "songlist-backend"),
        )

    @property
    def enabled(self) -> bool:
        return self.app is not None

    def initialize(self) -> bool:
        """Initialize Firebase with Application Default Credentials when configured."""
        if not self.database_url:
            return False

        try:
            self.app = firebase_admin.get_app(self.app_name)
            return True
        except ValueError:
            pass

        options: dict[str, Any] = {"databaseURL": self.database_url}
        if self.project_id:
            options["projectId"] = self.project_id

        self.app = firebase_admin.initialize_app(
            options=options,
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
            True,
        )

    async def delete_user(self, uid: str) -> None:
        if not self.app:
            raise RuntimeError("Firebase is not configured")
        await asyncio.to_thread(auth.delete_user, uid, app=self.app)

    async def close(self) -> None:
        if self.app and self._owns_app:
            await asyncio.to_thread(firebase_admin.delete_app, self.app)
        self.app = None
        self._owns_app = False

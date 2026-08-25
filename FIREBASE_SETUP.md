# Firebase personalization setup

The existing Gaana endpoints remain public. Routes under `/me` require a Firebase ID token and store each user's data in Firebase Realtime Database.

## 1. Create and configure Firebase

1. Create a Firebase project.
2. In **Authentication**, enable the sign-in providers used by the client application, such as Google or Email/Password.
3. Create a **Realtime Database** instance.
4. Deploy `firebase-database.rules.json` with `firebase deploy --only database`. The included `firebase.json` points the Firebase CLI to this rules file. These rules deny direct client access because this backend performs authorization and accesses the database through the Admin SDK.
5. In **Project settings → Service accounts**, create a service-account key for local development. Keep the downloaded JSON file outside this repository.

On Google Cloud, prefer Application Default Credentials supplied by the runtime instead of downloading a service-account key.

## 2. Configure the backend

Required environment variable:

```text
FIREBASE_DATABASE_URL=https://YOUR_DATABASE.REGION.firebasedatabase.app
```

Recommended variables:

```text
FIREBASE_PROJECT_ID=your-firebase-project-id
GOOGLE_APPLICATION_CREDENTIALS=C:\secure\path\firebase-service-account.json
CORS_ALLOW_ORIGINS=http://localhost:3000,http://localhost:5173
```

PowerShell example:

```powershell
$env:FIREBASE_DATABASE_URL="https://YOUR_DATABASE.REGION.firebasedatabase.app"
$env:FIREBASE_PROJECT_ID="your-firebase-project-id"
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\secure\firebase-service-account.json"
$env:CORS_ALLOW_ORIGINS="http://localhost:3000,http://localhost:5173"
python -m uvicorn app:app --reload
```

If `FIREBASE_DATABASE_URL` is absent, the original public catalog endpoints continue to run and personalized endpoints return HTTP 503.

For Docker, `GOOGLE_APPLICATION_CREDENTIALS` must be a path inside the container. Mount the service-account file read-only at that path, or use workload credentials in the deployment platform.

## 3. Authenticate client requests

The frontend signs in using a Firebase client SDK, obtains the current user's ID token, and sends it to this backend:

```javascript
const idToken = await auth.currentUser.getIdToken();

const response = await fetch("http://localhost:8000/me/recommendations", {
  headers: { Authorization: `Bearer ${idToken}` },
});
```

The API verifies that the token is valid, current, not revoked, and issued by the configured Firebase project. The UID from the verified token determines the database location; the client cannot choose another user's UID.

## Personalized API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/me` | Firebase account summary and music profile |
| `GET` | `/me/profile` | Read language, genre and artist preferences |
| `PATCH` | `/me/profile` | Update preferences |
| `GET` | `/me/favorites` | List favorite tracks |
| `POST` | `/me/favorites` | Save a track and strengthen its preference signals |
| `DELETE` | `/me/favorites/{seokey}` | Remove a favorite |
| `GET` | `/me/history` | Get recent listening events |
| `POST` | `/me/history` | Record playback and learn from it |
| `DELETE` | `/me/history` | Clear listening events |
| `DELETE` | `/me/signals` | Reset learned artist, genre and language weights |
| `GET` | `/me/recommendations` | Rank Gaana tracks using the learned profile |

Update a profile:

```json
{
  "display_name": "Seban",
  "languages": ["English", "Malayalam"],
  "favorite_genres": ["Rock", "Indie"],
  "favorite_artists": ["Coldplay", "The Local Train"]
}
```

Save a favorite or record history using the corresponding fields returned by the public song API. Comma-separated `artists`, `artist_ids`, and `genres` are accepted and normalized into arrays:

```json
{
  "seokey": "yellow",
  "track_id": "123",
  "title": "Yellow",
  "artists": "Coldplay",
  "artist_ids": "10",
  "genres": "Alternative, Rock",
  "language": "English",
  "album": "Parachutes"
}
```

For `/me/history`, the same body may additionally contain `played_seconds`, `completed`, and `source`.

## Realtime Database layout

```text
users/{encodedFirebaseUid}/
  account/                  # trusted identity snapshot
  profile/                  # explicit preferences
  favorites/tracks/{seokey}
  history/{pushId}
  signals/
    artists/{hash}
    genres/{hash}
    languages/{hash}
```

Recommendations combine explicit profile choices, favorite tracks, learned playback signals, preferred-language trending tracks, and the existing Gaana search API. Favorites are excluded and recently played tracks are demoted.

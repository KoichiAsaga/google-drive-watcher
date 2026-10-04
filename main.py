import os
import time
import uuid
import secrets

import requests
from dotenv import load_dotenv

from flask import (
    Flask,
    request,
    redirect,
    jsonify,
)

from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build


# ============================================================
# 初期化
# ============================================================

load_dotenv()

app = Flask(__name__)


# ============================================================
# 環境変数
# ============================================================

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

# 初回移行用
# Firestoreにrefresh_tokenが存在すればそちらを優先
INITIAL_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

TARGET_FOLDER_ID = os.environ["TARGET_FOLDER_ID"]
CHAT_SPACE_ID = os.environ["CHAT_SPACE_ID"]

CHANNEL_TOKEN = os.environ["CHANNEL_TOKEN"]

BASE_URL = os.environ["BASE_URL"].rstrip("/")

# 管理API用
AUTH_SETUP_TOKEN = os.environ["AUTH_SETUP_TOKEN"]
FIRESTOE_DB_NAME = os.environ["FIRESTOE_DB_NAME"]

# ============================================================
# OAuth Scopes
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
]


# ============================================================
# Firestore
# ============================================================

db = firestore.Client(database=FIRESTOE_DB_NAME)


# DriveのpageToken + 現在のwatch情報
DRIVE_STATE_DOCUMENT = (
    db.collection("drive_notifier")
    .document("state")
)


# refresh token
OAUTH_TOKEN_DOCUMENT = (
    db.collection("oauth")
    .document("google")
)


# OAuth PKCE途中状態
OAUTH_STATE_COLLECTION = (
    db.collection("oauth_states")
)


# Chatへまだ通知していないファイル
PENDING_COLLECTION = (
    db.collection("drive_pending_notifications")
)


# ============================================================
# Refresh Token
# ============================================================

def save_refresh_token(refresh_token: str):
    OAUTH_TOKEN_DOCUMENT.set(
        {
            "refresh_token": refresh_token,
        },
        merge=True,
    )


def load_refresh_token() -> str:
    snapshot = OAUTH_TOKEN_DOCUMENT.get()

    if snapshot.exists:
        data = snapshot.to_dict()

        refresh_token = data.get(
            "refresh_token"
        )

        if refresh_token:
            return refresh_token

    # 初回移行用
    if INITIAL_REFRESH_TOKEN:
        save_refresh_token(
            INITIAL_REFRESH_TOKEN
        )

        return INITIAL_REFRESH_TOKEN

    raise RuntimeError(
        "Google refresh token がありません。"
        f"{BASE_URL}/auth/start?token=..."
        " からOAuth認証してください。"
    )


# ============================================================
# Google Credentials
# ============================================================

def get_google_credentials():
    refresh_token = load_refresh_token()

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    try:
        credentials.refresh(
            Request()
        )

    except RefreshError as e:
        print(
            "OAuth refresh failed:",
            repr(e),
        )

        raise RuntimeError(
            "refresh token が失効しています。"
            f"{BASE_URL}/auth/start?token=..."
            " から再認証してください。"
        ) from e

    return credentials


# ============================================================
# Drive API
# ============================================================

def get_drive_service():
    credentials = (
        get_google_credentials()
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
    )


# ============================================================
# Google Chat
# ============================================================

def send_chat(message: str):
    credentials = (
        get_google_credentials()
    )

    url = (
        "https://chat.googleapis.com/v1/"
        f"spaces/{CHAT_SPACE_ID}/messages"
    )

    response = requests.post(
        url,
        headers={
            "Authorization":
                f"Bearer {credentials.token}",
            "Content-Type":
                "application/json",
        },
        json={
            "text": message,
        },
        timeout=10,
    )

    print(
        "Chat status:",
        response.status_code,
    )

    print(
        "Chat response:",
        response.text,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# OAuth Flow
# ============================================================

def create_oauth_flow(
    *,
    state=None,
    code_verifier=None,
):
    client_config = {
        "web": {
            "client_id":
                GOOGLE_CLIENT_ID,

            "client_secret":
                GOOGLE_CLIENT_SECRET,

            "auth_uri":
                "https://accounts.google.com/o/oauth2/auth",

            "token_uri":
                "https://oauth2.googleapis.com/token",

            "redirect_uris": [
                f"{BASE_URL}/auth/callback"
            ],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
        code_verifier=code_verifier,
    )

    flow.redirect_uri = (
        f"{BASE_URL}/auth/callback"
    )

    return flow


# ============================================================
# TARGET_DIR配下判定
# ============================================================

def is_under_target_folder(
    drive,
    parent_ids,
    parent_cache,
):
    """
    ファイルの親を上へ辿り、
    TARGET_FOLDER_IDが祖先に存在するか確認。

    parent_cacheを使って、
    同じフォルダに対するDrive API呼び出しを減らす。
    """

    queue = list(parent_ids)

    visited = set()

    while queue:

        folder_id = queue.pop()

        # TARGET_DIRに到達
        if folder_id == TARGET_FOLDER_ID:
            return True

        if folder_id in visited:
            continue

        visited.add(folder_id)

        # キャッシュあり
        if folder_id in parent_cache:

            queue.extend(
                parent_cache[
                    folder_id
                ]
            )

            continue

        try:
            folder = (
                drive.files()
                .get(
                    fileId=folder_id,
                    fields="id,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )

        except Exception as e:
            print(
                "Failed to get parent:",
                folder_id,
                repr(e),
            )

            parent_cache[
                folder_id
            ] = []

            continue

        parents = folder.get(
            "parents",
            [],
        )

        parent_cache[
            folder_id
        ] = parents

        queue.extend(
            parents
        )

    return False


# ============================================================
# Pending notification
# ============================================================

def add_pending_file(file_data):
    """
    通知待ちファイルをFirestoreに保存。

    document IDをfile_idにすることで、
    同じファイルへの連続変更は1件にまとめる。
    """

    file_id = file_data["id"]

    PENDING_COLLECTION.document(
        file_id
    ).set(
        {
            "file_id": file_id,

            "name":
                file_data.get(
                    "name",
                    "(unknown)",
                ),

            "url":
                file_data.get(
                    "webViewLink",
                    "",
                ),

            "updated_at":
                firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


# ============================================================
# Health Check
# ============================================================

@app.get("/")
def hello():
    return {
        "status": "ok",
        "service": "TARGET_DIR Drive notifier",
    }


# ============================================================
# OAuth Status
# ============================================================

@app.get("/auth/status")
def auth_status():
    snapshot = (
        OAUTH_TOKEN_DOCUMENT.get()
    )

    exists = False

    if snapshot.exists:
        exists = bool(
            snapshot
            .to_dict()
            .get("refresh_token")
        )

    return jsonify({
        "refresh_token_exists":
            exists
    })


# ============================================================
# OAuth Start
# ============================================================

@app.get("/auth/start")
def auth_start():

    admin_token = (
        request.args.get("token")
    )

    if (
        admin_token
        != AUTH_SETUP_TOKEN
    ):
        return "unauthorized", 401

    # PKCE
    code_verifier = (
        secrets.token_urlsafe(64)
    )

    flow = create_oauth_flow(
        code_verifier=code_verifier,
    )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
    )

    # Callbackで復元する
    OAUTH_STATE_COLLECTION.document(
        state
    ).set({
        "code_verifier":
            code_verifier,
    })

    return redirect(
        authorization_url
    )


# ============================================================
# OAuth Callback
# ============================================================

@app.get("/auth/callback")
def auth_callback():

    callback_state = (
        request.args.get("state")
    )

    authorization_code = (
        request.args.get("code")
    )

    oauth_error = (
        request.args.get("error")
    )

    if oauth_error:
        return jsonify({
            "error": oauth_error
        }), 400

    if not callback_state:
        return "state missing", 400

    if not authorization_code:
        return (
            "authorization code missing",
            400,
        )

    state_document = (
        OAUTH_STATE_COLLECTION
        .document(callback_state)
    )

    snapshot = (
        state_document.get()
    )

    if not snapshot.exists:
        return (
            "OAuth state not found",
            400,
        )

    data = snapshot.to_dict()

    code_verifier = (
        data.get("code_verifier")
    )

    if not code_verifier:
        return (
            "code_verifier missing",
            400,
        )

    flow = create_oauth_flow(
        state=callback_state,
        code_verifier=code_verifier,
    )

    try:
        flow.fetch_token(
            code=authorization_code
        )

    except Exception as e:
        print(
            "OAuth token exchange failed:",
            repr(e),
        )

        return jsonify({
            "error":
                "OAuth token exchange failed",

            "detail":
                str(e),
        }), 500

    credentials = (
        flow.credentials
    )

    refresh_token = (
        credentials.refresh_token
    )

    if not refresh_token:
        return (
            "refresh token not returned",
            500,
        )

    save_refresh_token(
        refresh_token
    )

    # PKCEの一時情報削除
    state_document.delete()

    return (
        "OAuth completed. "
        "Refresh token saved."
    )


# ============================================================
# Drive Watch登録
# ============================================================

@app.post("/register-watch")
def register_watch():

    admin_token = (
        request.headers.get(
            "X-Admin-Token"
        )
    )

    if (
        admin_token
        != AUTH_SETUP_TOKEN
    ):
        return "unauthorized", 401

    drive = get_drive_service()

    state_snapshot = (
        DRIVE_STATE_DOCUMENT.get()
    )

    state_data = {}

    if state_snapshot.exists:
        state_data = (
            state_snapshot.to_dict()
        )

    old_channel_id = (
        state_data.get(
            "channel_id"
        )
    )

    old_resource_id = (
        state_data.get(
            "resource_id"
        )
    )

    page_token = (
        state_data.get(
            "page_token"
        )
    )

    # ========================================================
    # 古いwatchを停止
    # ========================================================

    if (
        old_channel_id
        and old_resource_id
    ):

        try:
            (
                drive.channels()
                .stop(
                    body={
                        "id":
                            old_channel_id,

                        "resourceId":
                            old_resource_id,
                    }
                )
                .execute()
            )

            print(
                "Old watch stopped:",
                old_channel_id,
            )

        except Exception as e:
            # 期限切れ済みでも新watchは作る
            print(
                "Could not stop old watch:",
                repr(e),
            )

    # ========================================================
    # 初回だけpage token取得
    # ========================================================

    if not page_token:

        page_token = (
            drive.changes()
            .getStartPageToken()
            .execute()[
                "startPageToken"
            ]
        )

    # ========================================================
    # 新watch
    # ========================================================

    channel_id = str(
        uuid.uuid4()
    )

    # 6日間
    expiration_ms = int(
        (
            time.time()
            + 6 * 24 * 60 * 60
        )
        * 1000
    )

    result = (
        drive.changes()
        .watch(
            pageToken=page_token,
            body={
                "id":
                    channel_id,

                "type":
                    "web_hook",

                "address":
                    f"{BASE_URL}/drive-webhook",

                "token":
                    CHANNEL_TOKEN,

                "expiration":
                    str(expiration_ms),
            },
        )
        .execute()
    )

    resource_id = (
        result.get(
            "resourceId"
        )
    )

    expiration = (
        result.get(
            "expiration"
        )
    )

    # ========================================================
    # 現行watchを保存
    # ========================================================

    DRIVE_STATE_DOCUMENT.set(
        {
            "page_token":
                page_token,

            "channel_id":
                channel_id,

            "resource_id":
                resource_id,

            "expiration":
                expiration,
        },
        merge=True,
    )

    return jsonify({
        "message":
            "watch registered",

        "channel_id":
            channel_id,

        "resource_id":
            resource_id,

        "expiration":
            expiration,
    })


# ============================================================
# Drive Webhook
# ============================================================

@app.post("/drive-webhook")
def drive_webhook():

    print(
        "Drive webhook received"
    )

    # ========================================================
    # Channel Token
    # ========================================================

    received_token = (
        request.headers.get(
            "X-Goog-Channel-Token"
        )
    )

    if (
        received_token
        != CHANNEL_TOKEN
    ):

        print(
            "Ignoring invalid channel token"
        )

        # Googleに再試行させない
        return "", 204

    # ========================================================
    # Channel ID
    # ========================================================

    received_channel_id = (
        request.headers.get(
            "X-Goog-Channel-ID"
        )
    )

    state = (
        DRIVE_STATE_DOCUMENT.get()
    )

    if not state.exists:
        print(
            "Drive state missing"
        )

        return "", 204

    state_data = (
        state.to_dict()
    )

    active_channel_id = (
        state_data.get(
            "channel_id"
        )
    )

    # 古いwatchから来た通知
    if (
        active_channel_id
        and received_channel_id
        != active_channel_id
    ):

        print(
            "Ignoring old channel:",
            received_channel_id,
        )

        # Drive APIを叩かない
        return "", 204

    page_token = (
        state_data.get(
            "page_token"
        )
    )

    if not page_token:
        print(
            "page_token missing"
        )

        return "", 204

    # ========================================================
    # ここから初めてDrive APIを使う
    # ========================================================

    drive = get_drive_service()

    # 同じWebhook内で親フォルダ検索をキャッシュ
    parent_cache = {}

    detected_count = 0

    while page_token:

        result = (
            drive.changes()
            .list(
                pageToken=page_token,
                spaces="drive",
                fields=(
                    "nextPageToken,"
                    "newStartPageToken,"
                    "changes("
                    "fileId,"
                    "removed,"
                    "file("
                    "id,"
                    "name,"
                    "parents,"
                    "mimeType,"
                    "webViewLink"
                    ")"
                    ")"
                ),
            )
            .execute()
        )

        changes = (
            result.get(
                "changes",
                [],
            )
        )

        for change in changes:

            # 削除は無視
            if change.get(
                "removed"
            ):
                continue

            file_data = (
                change.get("file")
            )

            if not file_data:
                continue

            # フォルダ自体の変更は通知しない
            if (
                file_data.get(
                    "mimeType"
                )
                ==
                "application/vnd.google-apps.folder"
            ):
                continue

            parents = (
                file_data.get(
                    "parents",
                    [],
                )
            )

            # =================================================
            # TARGET_DIR配下か？
            # =================================================

            if not is_under_target_folder(
                drive,
                parents,
                parent_cache,
            ):
                continue

            print(
                "Detected:",
                file_data.get("name"),
            )

            # =================================================
            # ★ ここではChatへ送らない
            #
            # Firestoreに通知待ちとして保存するだけ
            # =================================================

            add_pending_file(
                file_data
            )

            detected_count += 1

        next_page_token = (
            result.get(
                "nextPageToken"
            )
        )

        if next_page_token:

            page_token = (
                next_page_token
            )

            continue

        new_token = (
            result.get(
                "newStartPageToken"
            )
        )

        if new_token:

            DRIVE_STATE_DOCUMENT.set(
                {
                    "page_token":
                        new_token
                },
                merge=True,
            )

        break

    print(
        "Pending files added:",
        detected_count,
    )

    return "", 204


# ============================================================
# 通知バッチ送信
# ============================================================

@app.post("/flush-notifications")
def flush_notifications():
    """
    Cloud Schedulerから10分ごとに呼ぶ。

    Firestoreに溜まっているファイルを
    まとめてGoogle Chatへ1通送る。
    """

    admin_token = (
        request.headers.get(
            "X-Admin-Token"
        )
    )

    if (
        admin_token
        != AUTH_SETUP_TOKEN
    ):
        return "unauthorized", 401

    # ========================================================
    # pending取得
    # ========================================================

    documents = list(
        PENDING_COLLECTION.stream()
    )

    if not documents:

        print(
            "No pending notifications"
        )

        return jsonify({
            "message":
                "nothing to notify",

            "count":
                0,
        })

    files = []

    for document in documents:

        data = (
            document.to_dict()
        )

        files.append({
            "id":
                document.id,

            "name":
                data.get(
                    "name",
                    "(unknown)",
                ),

            "url":
                data.get(
                    "url",
                    "",
                ),
        })

    # 名前順
    files.sort(
        key=lambda x:
            x["name"].lower()
    )

    total = len(files)

    # Chatを巨大化させない
    max_display = 30

    visible_files = (
        files[:max_display]
    )

    lines = [
        (
            "📁 Google Driveに"
            f"{total}件のファイル変更があります"
        ),
        "",
    ]

    for file_data in visible_files:

        name = file_data["name"]
        url = file_data["url"]

        if url:
            lines.append(
                f"・{name}\n  {url}"
            )
        else:
            lines.append(
                f"・{name}"
            )

    if total > max_display:

        remaining = (
            total - max_display
        )

        lines.append("")
        lines.append(
            f"ほか {remaining} 件"
        )

    message = "\n".join(
        lines
    )

    # ========================================================
    # Chat送信
    #
    # 成功するまでFirestoreから消さない
    # ========================================================

    send_chat(
        message
    )

    # ========================================================
    # 送信成功後にpending削除
    # ========================================================

    # Firestore batchは最大500 writesなので
    # 400件ずつ処理
    for start in range(
        0,
        len(documents),
        400,
    ):

        batch = db.batch()

        chunk = documents[
            start:start + 400
        ]

        for document in chunk:
            batch.delete(
                document.reference
            )

        batch.commit()

    print(
        "Notification batch sent:",
        total,
    )

    return jsonify({
        "message":
            "notification sent",

        "count":
            total,
    })

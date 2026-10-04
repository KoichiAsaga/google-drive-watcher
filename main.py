import os
import uuid
import secrets

import requests
from dotenv import load_dotenv
from flask import Flask, request, redirect, jsonify

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

# Firestoreにまだrefresh tokenが存在しない場合だけ使う
INITIAL_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

TARGET_FOLDER_ID = os.environ["TARGET_FOLDER_ID"]
CHAT_SPACE_ID = os.environ["CHAT_SPACE_ID"]

CHANNEL_TOKEN = os.environ["CHANNEL_TOKEN"]

BASE_URL = os.environ["BASE_URL"].rstrip("/")

# /auth/start や /register-watch を誰でも呼べないようにする
AUTH_SETUP_TOKEN = os.environ["AUTH_SETUP_TOKEN"]


# ============================================================
# OAuth Scope
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.messages.readonly"
]


# ============================================================
# Firestore
# ============================================================

# NOTE: databaseの名前をgcp上に実在するDBにすること. 

db = firestore.Client(database="aob-db")


# Drive changes の現在位置
DRIVE_STATE_DOCUMENT = (
    db.collection("drive_notifier")
    .document("state")
)


# refresh token保存先
OAUTH_TOKEN_DOCUMENT = (
    db.collection("oauth")
    .document("google")
)


# OAuth認証途中のstate / code_verifier保存先
#
# oauth_states
#   └─ <state>
#        └─ code_verifier: "..."
#
OAUTH_STATE_COLLECTION = db.collection("oauth_states")


# ============================================================
# Refresh Token 管理
# ============================================================

def save_refresh_token(refresh_token: str):
    """
    refresh tokenをFirestoreへ保存する。
    """

    OAUTH_TOKEN_DOCUMENT.set(
        {
            "refresh_token": refresh_token
        },
        merge=True,
    )


def load_refresh_token() -> str:
    """
    Firestoreからrefresh tokenを取得する。

    Firestoreにない場合のみ、
    GOOGLE_REFRESH_TOKEN環境変数から初期移行する。
    """

    snapshot = OAUTH_TOKEN_DOCUMENT.get()

    if snapshot.exists:
        data = snapshot.to_dict()

        refresh_token = data.get("refresh_token")

        if refresh_token:
            return refresh_token

    # 初回移行用
    if INITIAL_REFRESH_TOKEN:
        save_refresh_token(INITIAL_REFRESH_TOKEN)

        return INITIAL_REFRESH_TOKEN

    raise RuntimeError(
        "Google refresh token が存在しません。"
        f"{BASE_URL}/auth/start?token=... から再認証してください。"
    )


# ============================================================
# Google Credentials
# ============================================================

def get_google_credentials():
    """
    refresh tokenを使ってaccess tokenを取得する。
    """

    refresh_token = load_refresh_token()

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    try:
        credentials.refresh(Request())

    except RefreshError as e:
        print(
            "Google OAuth refresh failed:",
            repr(e),
        )

        raise RuntimeError(
            "Google refresh token が失効しています。"
            "OAuth再認証が必要です。\n"
            f"{BASE_URL}/auth/start?token=AUTH_SETUP_TOKEN"
        ) from e

    return credentials


# ============================================================
# Drive API
# ============================================================

def get_drive_service():
    credentials = get_google_credentials()

    return build(
        "drive",
        "v3",
        credentials=credentials,
    )


# ============================================================
# Google Chat API
# ============================================================

def send_chat(message: str):
    credentials = get_google_credentials()

    url = (
        "https://chat.googleapis.com/v1/"
        f"spaces/{CHAT_SPACE_ID}/messages"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": (
                f"Bearer {credentials.token}"
            ),
            "Content-Type": "application/json",
        },
        json={
            "text": message
        },
        timeout=10,
    )

    print("Chat status:", response.status_code)
    print("Chat response:", response.text)

    response.raise_for_status()

    return response.json()


# ============================================================
# OAuth Flow生成
# ============================================================

def create_oauth_flow(
    *,
    state=None,
    code_verifier=None,
):
    """
    Google OAuth Flowを生成する。

    code_verifierを渡すことでPKCEを利用する。
    """

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": (
                "https://accounts.google.com/o/oauth2/auth"
            ),
            "token_uri": (
                "https://oauth2.googleapis.com/token"
            ),
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
# Health Check
# ============================================================

@app.get("/")
def hello():
    return "Drive notifier is running"


# ============================================================
# OAuth状態確認
# ============================================================

@app.get("/auth/status")
def auth_status():
    """
    refresh tokenが保存されているかだけ確認する。

    refresh tokenそのものは返さない。
    """

    snapshot = OAUTH_TOKEN_DOCUMENT.get()

    exists = False

    if snapshot.exists:
        exists = bool(
            snapshot.to_dict().get(
                "refresh_token"
            )
        )

    return jsonify({
        "refresh_token_exists": exists
    })


# ============================================================
# OAuth開始
# ============================================================

@app.get("/auth/start")
def auth_start():
    """
    OAuth認証を開始する。

    例:
    /auth/start?token=xxxxxxxx
    """

    admin_token = request.args.get("token")

    if admin_token != AUTH_SETUP_TOKEN:
        return "unauthorized", 401

    # --------------------------------------------------------
    # PKCE用の秘密値
    #
    # Googleにはこの値そのものではなく
    # SHA256した code_challenge が送られる。
    # --------------------------------------------------------

    code_verifier = secrets.token_urlsafe(64)

    flow = create_oauth_flow(
        code_verifier=code_verifier,
    )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",

            # refresh tokenを再発行させる
            prompt="consent",

            include_granted_scopes="true",
        )
    )

    # --------------------------------------------------------
    # callbackで必要になるので保存する
    #
    # stateごとにドキュメントを分けることで
    # 複数リクエストでも衝突しにくくする
    # --------------------------------------------------------

    OAUTH_STATE_COLLECTION.document(
        state
    ).set({
        "code_verifier": code_verifier
    })

    print(
        f"OAuth started. state={state}"
    )

    return redirect(authorization_url)


# ============================================================
# OAuth Callback
# ============================================================

@app.get("/auth/callback")
def auth_callback():
    """
    Googleログイン後にGoogleから呼ばれる。

    authorization code + code_verifier
          ↓
    access token / refresh token
    """

    callback_state = request.args.get(
        "state"
    )

    authorization_code = request.args.get(
        "code"
    )

    oauth_error = request.args.get(
        "error"
    )

    # Google側で拒否された場合
    if oauth_error:
        return jsonify({
            "error": oauth_error
        }), 400

    if not callback_state:
        return "state missing", 400

    if not authorization_code:
        return "authorization code missing", 400

    # --------------------------------------------------------
    # /auth/start時に保存したcode_verifierを取得
    # --------------------------------------------------------

    state_document = (
        OAUTH_STATE_COLLECTION
        .document(callback_state)
    )

    state_snapshot = state_document.get()

    if not state_snapshot.exists:
        return (
            "OAuth state not found. "
            "Please restart OAuth flow.",
            400,
        )

    state_data = state_snapshot.to_dict()

    code_verifier = state_data.get(
        "code_verifier"
    )

    if not code_verifier:
        return (
            "PKCE code_verifier not found.",
            400,
        )

    # --------------------------------------------------------
    # 同じstate / code_verifierでFlowを復元
    # --------------------------------------------------------

    flow = create_oauth_flow(
        state=callback_state,
        code_verifier=code_verifier,
    )

    try:
        # authorization code
        # +
        # PKCE code verifier
        #
        # をGoogleのToken Endpointへ送る
        flow.fetch_token(
            code=authorization_code
        )

    except Exception as e:
        print(
            "OAuth token exchange failed:",
            repr(e),
        )

        return jsonify({
            "error": "OAuth token exchange failed",
            "detail": str(e),
        }), 500

    credentials = flow.credentials

    refresh_token = credentials.refresh_token

    if not refresh_token:
        return jsonify({
            "error": (
                "Google did not return "
                "a refresh token."
            )
        }), 500

    # --------------------------------------------------------
    # 新refresh tokenをFirestoreへ保存
    # --------------------------------------------------------

    save_refresh_token(
        refresh_token
    )

    # PKCE verifierは一回使ったら不要
    state_document.delete()

    print(
        "New Google refresh token stored."
    )

    return (
        "Google OAuth authentication completed. "
        "New refresh token has been saved."
    )


# ============================================================
# Drive Watch登録
# ============================================================

def is_under_target_folder(drive, parent_ids):
    """
    parent_ids から親フォルダを上へ辿り、
    TARGET_FOLDER_ID が祖先に存在するか確認する。

    例:
        TARGET_DIR
          └── live
                └── 2026
                      └── song.pdf

    song.pdf の直接の親が 2026 でも、
    祖先に TARGET_DIR があれば True。
    """

    visited = set()
    queue = list(parent_ids)

    while queue:
        folder_id = queue.pop()

        # 監視対象フォルダに到達
        if folder_id == TARGET_FOLDER_ID:
            return True

        # 同じフォルダを何度も調べない
        if folder_id in visited:
            continue

        visited.add(folder_id)

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
                f"Failed to get parent folder "
                f"{folder_id}: {repr(e)}"
            )
            continue

        parent_folders = folder.get(
            "parents",
            [],
        )

        queue.extend(parent_folders)

    return False


@app.post("/register-watch")
def register_watch():
    """
    Drive changes.watch を登録する。

    X-Admin-Token:
        AUTH_SETUP_TOKEN
    """

    admin_token = request.headers.get(
        "X-Admin-Token"
    )

    if admin_token != AUTH_SETUP_TOKEN:
        return "unauthorized", 401

    drive = get_drive_service()

    # --------------------------------------------------------
    # 現在のDrive変更履歴の位置を取得
    # --------------------------------------------------------

    start_page_token = (
        drive.changes()
        .getStartPageToken()
        .execute()["startPageToken"]
    )

    DRIVE_STATE_DOCUMENT.set({
        "page_token": start_page_token
    })

    channel_id = str(
        uuid.uuid4()
    )

    # --------------------------------------------------------
    # Driveに
    #
    # 「変更があったら /drive-webhook を呼んで」
    #
    # と登録
    # --------------------------------------------------------

    result = (
        drive.changes()
        .watch(
            pageToken=start_page_token,
            body={
                "id": channel_id,
                "type": "web_hook",
                "address": (
                    f"{BASE_URL}/drive-webhook"
                ),
                "token": CHANNEL_TOKEN,
            },
        )
        .execute()
    )

    return jsonify({
        "message": "watch registered",
        "channel_id": channel_id,
        "result": result,
    })


# ============================================================
# Drive Webhook
# ============================================================

@app.post("/drive-webhook")
def drive_webhook():

    print("Drive webhook received")

    # ========================================================
    # 1. Channel Token確認
    # ========================================================

    received_token = request.headers.get(
        "X-Goog-Channel-Token"
    )

    if received_token != CHANNEL_TOKEN:

        print(
            "Invalid channel token"
        )

        return "", 204

    # ========================================================
    # 2. Channel ID確認
    # ========================================================

    received_channel_id = (
        request.headers.get(
            "X-Goog-Channel-ID"
        )
    )

    state = (
        DRIVE_STATE_DOCUMENT
        .get()
    )

    if not state.exists:
        print(
            "Drive state not found"
        )
        return "", 204

    state_data = state.to_dict()

    active_channel_id = (
        state_data.get(
            "channel_id"
        )
    )

    # 古いwatchから来た通知なら
    # Drive APIすら叩かず終了する
    if (
        active_channel_id
        and received_channel_id
        != active_channel_id
    ):

        print(
            "Ignoring old Drive channel:",
            received_channel_id,
            "active:",
            active_channel_id,
        )

        return "", 204

    # ========================================================
    # ここから本来の処理
    # ========================================================

    page_token = state_data.get(
        "page_token"
    )

    if not page_token:
        print(
            "Drive page token not found"
        )
        return "", 204

    drive = get_drive_service()

    # ========================================================
    # Drive変更履歴を取得
    # ========================================================

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

        changes = result.get(
            "changes",
            [],
        )

        # ====================================================
        # 変更されたファイルを1件ずつ確認
        # ====================================================

        for change in changes:

            # 削除されたファイルは通知しない
            if change.get("removed"):
                continue

            file_data = change.get(
                "file"
            )

            if not file_data:
                continue

            file_id = file_data.get(
                "id"
            )

            name = file_data.get(
                "name",
                "(unknown)"
            )

            mime_type = file_data.get(
                "mimeType",
                ""
            )

            parents = file_data.get(
                "parents",
                [],
            )

            url = file_data.get(
                "webViewLink",
                "",
            )

            # =================================================
            # フォルダ自身の変更は通知しない
            # =================================================

            if (
                mime_type
                == "application/vnd.google-apps.folder"
            ):
                print(
                    f"Skip folder change: {name}"
                )
                continue

            # =================================================
            # TARGET_DIR配下か確認
            #
            # 直接の親だけでなく、
            # 親 → 親 → 親...
            # と上へ辿る。
            # =================================================

            if not is_under_target_folder(
                drive,
                parents,
            ):
                print(
                    f"Skip outside target folder: "
                    f"{name}"
                )
                continue

            # =================================================
            # 対象ファイル
            # =================================================

            print(
                f"Detected target file: "
                f"{name} ({file_id})"
            )

            # =================================================
            # Google Chatへ通知
            # =================================================

            message = (
                "📁 Google Driveに"
                "ファイルが追加・変更されました\n"
                f"{name}\n"
                f"{url}"
            )

            try:
                send_chat(message)

            except Exception as e:
                print(
                    f"Failed to send Chat message: "
                    f"{repr(e)}"
                )

                # Chat送信失敗を握り潰したくないなら
                # raise に変更してもOK
                raise

        # ====================================================
        # changes.list のページング
        # ====================================================

        next_page_token = result.get(
            "nextPageToken"
        )

        if next_page_token:
            page_token = next_page_token
            continue

        # ====================================================
        # 全変更を読み終えたので、
        # 次回用のpage tokenを保存
        # ====================================================

        new_start_page_token = result.get(
            "newStartPageToken"
        )

        if new_start_page_token:
            DRIVE_STATE_DOCUMENT.set({
                "page_token":
                    new_start_page_token
            })

        break

    return "", 204


@app.get("/chat-space-test")
def chat_space_test():
    credentials = get_google_credentials()

    url = (
        "https://chat.googleapis.com/v1/"
        f"spaces/{CHAT_SPACE_ID}/messages"
    )

    response = requests.get(
        url,
        headers={
            "Authorization": (
                f"Bearer {credentials.token}"
            )
        },
        timeout=10,
    )

    return (
        response.text,
        response.status_code,
        {"Content-Type": "application/json"},
    )


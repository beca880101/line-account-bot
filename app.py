# ================================================================
#  LINE 記帳機器人（Google Sheet + 報表 + 強健解析 + 防誤記）
# ================================================================

import os
import json
import datetime
import ast
import operator as op

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

# ================================================================
#  環境變數
# ================================================================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("缺少 LINE Token（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")

if not GOOGLE_SHEET_NAME:
    raise ValueError("請在環境變數設定 GOOGLE_SHEET_NAME（試算表名稱，例如：小木子秘書db）")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ================================================================
#  Google Sheet 連線
# ================================================================
def get_sheet():
    """取得 Google Sheet 物件（失敗回傳 None）"""
    if not GOOGLE_CREDENTIALS_JSON:
        print("【錯誤】未設定 GOOGLE_CREDENTIALS_JSON")
        return None

    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)

        sheet = client.open(GOOGLE_SHEET_NAME).sheet1

        # 若 Google Sheet 是空的，建立標題列
        if not sheet.get_all_values():
            sheet.append_row(["時間", "使用者ID", "群組ID", "金額", "備註", "原始指令"])

        return sheet

    except Exception as e:
        print("【Google Sheets 連線錯誤】", e)
        return None


# ================================================================
#  全形 → 半形
# ================================================================
def to_halfwidth(s):
    """將字串中的全形數字 / 符號轉為半形"""
    result = []
    for ch in s:
        code = ord(ch)
        # 全形空白
        if code == 0x3000:
            result.append(" ")
        # 全形字元（！到～）
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return "".join(result)


# ================================================================
#  安全運算式計算 (+ - * /)
# ================================================================
allowed_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

def safe_eval(expr):
    """安全的 + - * / 計算，不允許其它運算"""
    expr = expr.replace(" ", "")
    if not expr:
        raise ValueError("empty expr")

    def _eval(node):
        # Python 3.8+ 會用 Constant
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        # 相容舊版的 Num
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.BinOp):
            if type(node.op) not in allowed_ops:
                raise ValueError("bad op")
            return allowed_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in allowed_unary:
                raise ValueError("bad unary")
            return allowed_unary[type(node.op)](_eval(node.operand))
        raise ValueError("bad expr")

    tree = ast.parse(expr, mode="eval")
    return float(_eval(tree.body))


# ================================================================
#  記帳指令解析 (+100午餐 / +100 午餐 / +100*3-20 早餐 ...)
# ================================================================
def parse_transaction(text):
    """
    解析記帳指令。
    回傳 (amount, memo, expr_str)
    若不是合法記帳指令 → raise ValueError（外層會當作一般聊天忽略）
    """
    original = text.strip()
    s = to_halfwidth(original)

    # 必須以半形 + / - 開頭才視為記帳指令
    if not s or s[0] not in "+-":
        raise ValueError("not transaction")

    # 允許出現在運算式內的字元
    allowed_chars = set("0123456789+-*/(). ")

    expr_chars = []
    for ch in s:
        if ch in allowed_chars:
            expr_chars.append(ch)
        else:
            break

    expr_str = "".join(expr_chars).strip()

    # 運算式中至少要有一個數字，否則當作不是記帳
    if not expr_str or not any(c.isdigit() for c in expr_str):
        raise ValueError("no digits in expr")

    # 安全計算
    amount = safe_eval(expr_str)

    # 後面的全部當備註
    memo = s[len("".join(expr_chars)) :].strip()
    if not memo:
        memo = "無備註"

    return amount, memo, expr_str


# ================================================================
#  寫入 Google Sheet
# ================================================================
def write_record(user_id, group_id, amount, memo, raw_text):
    """寫入一筆紀錄，成功回傳 True，失敗回傳 False"""
    sheet = get_sheet()
    if not sheet:
        return False

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gid_to_save = group_id if group_id else "Private"

    sheet.append_row([now_str, user_id, gid_to_save, amount, memo, raw_text])
    return True


# ================================================================
#  取得某個聊天（私聊/群組）在指定月份的所有紀錄
# ================================================================
def get_transactions_for_context(sheet, user_id, group_id, year_month=None):
    """
    從 Google Sheet 取出指定聊天室的紀錄，依時間由新到舊。
    year_month 例如 "2025-11"；若為 None 則不過濾月份。
    """
    rows = sheet.get_all_records()
    records = []

    # 由最新開始看（反轉）
    for row in reversed(rows):
        r_time = str(row.get("時間", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_gid = str(row.get("群組ID", ""))
        r_amt = row.get("金額", 0)
        r_memo = str(row.get("備註", ""))

        # 月份過濾
        if year_month and not r_time.startswith(year_month):
            continue

        # 聊天室過濾
        if group_id:
            # 群組：只看群組ID一致的
            if r_gid != group_id:
                continue
        else:
            # 私訊：群組ID 必須是 "Private"，且 userId 要一致
            if not (r_gid == "Private" and r_uid == user_id):
                continue

        try:
            amount = float(r_amt)
        except Exception:
            continue

        # 顯示用時間格式：11/26 14:23
        display_time = r_time
        try:
            dt = datetime.datetime.strptime(r_time, "%Y-%m-%d %H:%M:%S")
            display_time = dt.strftime("%m/%d %H:%M")
        except Exception:
            pass

        records.append(
            {
                "time": display_time,
                "amount": amount,
                "memo": r_memo,
            }
        )

    return records


# ================================================================
#  計算該聊天室的總額（群組或私聊）
# ================================================================
def calc_balance(user_id, group_id):
    sheet = get_sheet()
    if not sheet:
        return None

    rows = sheet.get_all_records()
    bal = 0.0

    for row in rows:
        r_uid = str(row.get("使用者ID", ""))
        r_gid = str(row.get("群組ID", ""))
        r_amt = row.get("金額", 0)

        try:
            amt = float(r_amt)
        except Exception:
            continue

        if group_id:
            if r_gid == group_id:
                bal += amt
        else:
            if r_gid == "Private" and r_uid == user_id:
                bal += amt

    return bal


# ================================================================
#  Flex 卡片：記帳成功的小卡
# ================================================================
def build_transaction_flex(expr_str, memo, total):
    total_str = f"{total:.2f}".rstrip("0").rstrip(".")
    return FlexSendMessage(
        alt_text="記帳成功",
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "記帳成功",
                        "weight": "bold",
                        "size": "lg",
                    },
                    {
                        "type": "text",
                        "text": f"本次：{expr_str}",
                        "size": "sm",
                    },
                    {
                        "type": "text",
                        "text": f"備註：{memo}",
                        "size": "sm",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"目前總額：{total_str} 元",
                        "size": "md",
                        "weight": "bold",
                    },
                ],
            },
        },
    )


# ================================================================
#  Flex 卡片：本月近 10 筆報表
# ================================================================
def build_report_flex(records, month_label, monthly_total):
    """
    records: list of dicts {time, amount, memo} 最新在前
    """
    total_str = f"{monthly_total:.2f}".rstrip("0").rstrip(".")

    # 每一筆記錄一行
    rows_contents = []
    for r in records:
        amt = r["amount"]
        memo = r["memo"]
        time_str = r["time"]

        # 金額顯示：有 + / -
        sign = "+" if amt >= 0 else "-"
        amt_abs_str = f"{abs(amt):.2f}".rstrip("0").rstrip(".")

        row_box = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"{sign}{amt_abs_str}",
                    "size": "sm",
                    "flex": 2,
                },
                {
                    "type": "text",
                    "text": memo,
                    "size": "sm",
                    "flex": 5,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": time_str,
                    "size": "xs",
                    "flex": 3,
                    "align": "end",
                    "color": "#999999",
                },
            ],
        }
        rows_contents.append(row_box)

    if not rows_contents:
        rows_contents.append(
            {
                "type": "text",
                "text": "本月尚無紀錄",
                "size": "sm",
                "color": "#999999",
            }
        )

    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "📘 近 10 筆記帳紀錄",
                    "weight": "bold",
                    "size": "lg",
                },
                {
                    "type": "text",
                    "text": f"{month_label} 本月",
                    "size": "sm",
                    "color": "#666666",
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": rows_contents,
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": f"本月累積：{total_str} 元",
                    "size": "sm",
                    "weight": "bold",
                },
            ],
        },
    }

    return FlexSendMessage(alt_text="本月記帳報表", contents=bubble)


# ================================================================
#  LINE Webhook
# ================================================================
@app.route("/callback", methods=["POST", "HEAD"])
def callback():
    # 給 UptimeRobot / Render 健康檢查用
    if request.method == "HEAD":
        return ("", 200)

    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ================================================================
#  主訊息處理邏輯
# ================================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    group_id = event.source.group_id if event.source.type == "group" else None

    # -------- 指令：餘額 --------
    if text in ["餘額", "balance"]:
        bal = calc_balance(user_id, group_id)
        if bal is None:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Google Sheets 連線失敗，請稍後再試 🥲"),
            )
            return

        bal_rounded = round(bal, 2)
        if bal_rounded > 0:
            msg = f"目前小朋友欠 {bal_rounded} 元"
        elif bal_rounded < 0:
            msg = f"目前欠小朋友 {abs(bal_rounded)} 元"
        else:
            msg = "目前互不相欠 ✨"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # -------- 指令：報表 / report / excel --------
    cmd = text.strip().lower()
    if cmd in ["報表", "report", "excel"]:
        sheet = get_sheet()
        if not sheet:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Google Sheets 連線失敗，請稍後再試 🥲"),
            )
            return

        current_month = datetime.datetime.now().strftime("%Y-%m")
        all_records = get_transactions_for_context(
            sheet, user_id, group_id, year_month=current_month
        )

        if not all_records:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"{current_month} 本月尚無紀錄"),
            )
            return

        monthly_total = sum(r["amount"] for r in all_records)
        recent_10 = all_records[:10]

        flex = build_report_flex(recent_10, current_month, monthly_total)
        sheet_url = "https://docs.google.com/spreadsheets/d/" + sheet.spreadsheet.id
        summary = (
            f"📘 {current_month} 本月總結\n"
            f"筆數：{len(all_records)} 筆\n"
            f"總累積：{round(monthly_total, 2)} 元\n\n"
            f"🔗 完整紀錄請見試算表：\n{sheet_url}"
        )

        line_bot_api.reply_message(
            event.reply_token, [flex, TextSendMessage(text=summary)]
        )
        return

    # -------- 嘗試解析記帳指令 --------
    try:
        amount, memo, expr_str = parse_transaction(text)

        # 寫入 Google Sheet
        ok = write_record(user_id, group_id, amount, memo, text)
        if not ok:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Google Sheets 連線失敗，請稍後再試 🥲"),
            )
            return

        # 重算餘額
        bal = calc_balance(user_id, group_id)
        if bal is None:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Google Sheets 連線失敗，請稍後再試 🥲"),
            )
            return

        flex = build_transaction_flex(expr_str, memo, bal)
        line_bot_api.reply_message(event.reply_token, flex)
        return

    except ValueError:
        # 不是合法記帳指令 → 當作一般聊天，完全忽略（不寫入 Sheet、不回覆）
        return
    except Exception as e:
        print("【處理訊息時發生未預期錯誤】", e)
        # 安全起見，出錯時也不回覆使用者，避免訊息炸裂
        return


# ================================================================
#  Flask 啟動（本地測試用；Render 上會用 gunicorn）
# ================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

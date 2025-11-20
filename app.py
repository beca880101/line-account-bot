import os
import re
import ast
import operator as op
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

# ===== 從環境變數讀取 LINE Token（部署時在 Render 設定） =====
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("請先在環境變數設定 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")

# === 安全算式計算，只允許 + - * / 和括號 ===
allowed_ops = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
}
allowed_unary = {
    ast.UAdd: op.pos,
    ast.USub: op.neg,
}


def safe_eval_expr(expr: str) -> float:
    """
    安全地計算類似：-200*24.5-100*20 這種算式
    只允許：數字、+ - * /、括號、小數點
    解析失敗會丟出 ValueError
    """
    expr = expr.replace(" ", "")
    if not expr:
        raise ValueError("empty expression")

    def _eval(node):
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


def parse_expr_and_memo(raw: str):
    """
    模式 B：數字 / 算式直接接文字，但「一定要 + 或 - 開頭」才記帳
    例：
      +200牛肉麵   ✅ 會記帳（+200）
      -50交通費    ✅ 會記帳（-50）
      100牛肉麵    ❌ 不記帳（當成普通文字）
      我餓了       ❌ 不記帳

    前面連續的 +-*/().0-9 視為算式，後面全部是備註
    回傳：(delta: float, memo: str|None)
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty")

    # ⭐ 重點：沒有以 + 或 - 開頭就直接視為「不是記帳指令」
    if s[0] not in "+-":
        raise ValueError("no leading sign")

    allowed_chars = set("0123456789.+-*/()")
    expr_chars = []
    i = 0
    for i, ch in enumerate(s):
        if ch in allowed_chars:
            expr_chars.append(ch)
        else:
            break
    else:
        i += 1

    expr = "".join(expr_chars).strip()
    if not expr or not any(c.isdigit() for c in expr):
        raise ValueError("no numeric expr")

    memo = s[len(expr):].strip()
    delta = safe_eval_expr(expr)
    return delta, memo or None



app = Flask(__name__)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 個人 / 群組餘額都用 float
user_balances = {}          # {user_id: float}
group_accounts = {}         # {group_id: {"older":..., "younger":..., "balance": float}}

HELP_KEYWORDS = ["說明", "help", "指令", "使用說明"]


def format_group_balance(balance: float) -> str:
    balance = round(balance, 2)
    if balance > 0:
        return f"目前小朋友欠 {balance} 台幣。"
    elif balance < 0:
        return f"目前姐姐欠小朋友 {abs(balance)} 台幣。"
    else:
        return "目前互不相欠 ✨"


@app.route("/")
def index():
    # 給 Render 健康檢查用，也方便你自己測是不是活著
    return "Line accounting bot is running."


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


def build_settle_flex(
    prev_amount: float,
    delta: float,
    total: float,
    unit: str = "台幣",
    current_label: str = "目前欠款",
    memo: str | None = None
):
    """結算結果小卡片"""
    prev_amount = round(prev_amount, 2)
    delta = round(delta, 2)
    total = round(total, 2)

    sign = "+" if delta >= 0 else "-"
    delta_abs = abs(delta)

    memo_text = f"備註：{memo}" if memo else "備註："

    return FlexSendMessage(
        alt_text="計算結果",
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "計算結果",
                        "weight": "bold",
                        "size": "lg",
                        "color": "#2E7D32"
                    },
                    {
                        "type": "text",
                        "text": f"{sign}{delta_abs} = {total}",
                        "size": "sm",
                        "color": "#8D6E63",
                        "align": "end"
                    },
                    {
                        "type": "separator",
                        "margin": "md"
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "margin": "md",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "上次金額", "size": "sm"},
                                    {
                                        "type": "text",
                                        "text": f"{prev_amount} {unit}",
                                        "size": "sm",
                                        "align": "end",
                                        "color": "#8D6E63"
                                    }
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "本次金額", "size": "sm"},
                                    {
                                        "type": "text",
                                        "text": f"{delta} {unit}",
                                        "size": "sm",
                                        "align": "end",
                                        "color": "#8D6E63"
                                    }
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": current_label, "size": "sm"},
                                    {
                                        "type": "text",
                                        "text": f"{total} {unit}",
                                        "size": "sm",
                                        "align": "end",
                                        "color": "#8D6E63"
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "separator",
                        "margin": "md"
                    },
                    {
                        "type": "text",
                        "text": memo_text,
                        "size": "xs",
                        "color": "#B0BEC5",
                        "wrap": True
                    }
                ]
            }
        }
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    # /id 查自己 ID
    if text == "/id":
        uid = getattr(event.source, "user_id", "無法取得 userId")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"你的 userId 是：\n{uid}"))
        return

    # ================= 群組：姐姐 / 小朋友 模式 =================
    if event.source.type == "group":
        gid = event.source.group_id
        uid = event.source.user_id

        if gid not in group_accounts:
            group_accounts[gid] = {"older": None, "younger": None, "balance": 0.0}
        ga = group_accounts[gid]

        # 綁定身分（保留舊說法）
        if text in ["我是姐姐", "我是姊姊"]:
            ga["older"] = uid
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="你已綁定為【姐姐】"))
            return

        if text in ["我是小朋友", "我是妹妹"]:
            ga["younger"] = uid
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="你已綁定為【小朋友】"))
            return

        # 查餘額
        if text in ["餘額", "查餘額", "balance"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_group_balance(ga["balance"])))
            return

        # 清零
        if text in ["清帳", "reset"]:
            ga["balance"] = 0.0
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="已清帳。\n" + format_group_balance(ga["balance"]))
            )
            return

        # 試著當「算式 + 備註」解析
        try:
            delta, memo = parse_expr_and_memo(text)   # float，可正可負
        except Exception:
            # 不是算式 → 只有主動要說明才回
            if text in HELP_KEYWORDS:
                help_text = (
                    "👭 雙人記帳機器人使用說明（姐姐 / 小朋友）：\n"
                    "綁定：\n 姐姐→我是姐姐\n 小朋友→我是小朋友\n\n"
                    "記帳：可以直接輸入金額或算式＋備註，例如：\n"
                    "+200牛肉麵\n-50交通\n-200*24.5-100*20晚餐\n\n"
                    "規則：\n"
                    "  結果 > 0：小朋友欠姐姐\n"
                    "  結果 < 0：姐姐欠小朋友\n\n"
                    "查餘額：餘額\n清帳：清帳\n查 userId：/id"
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
            return

        # 還沒綁定就提示一次
        if ga["older"] is None or ga["younger"] is None:
            msg = (
                "請先在群組綁定身分：\n"
                "姐姐：我是姐姐\n小朋友：我是小朋友"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            return

        prev_bal = ga["balance"]
        ga["balance"] += delta
        new_bal = ga["balance"]

        # 根據目前總餘額決定「目前欠款」那一行的文字
        if new_bal > 0:
            label = "目前小朋友欠"
        elif new_bal < 0:
            label = "目前姐姐欠小朋友"
        else:
            label = "目前互不相欠"

        flex = build_settle_flex(
            prev_amount=prev_bal,
            delta=delta,
            total=new_bal,
            unit="台幣",
            current_label=label,
            memo=memo
        )
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # ================= 私聊：個人記帳 =================
    if event.source.type == "user":
        uid = event.source.user_id
        user_balances.setdefault(uid, 0.0)

        # 先當 算式 + 備註 處理（+100牛肉麵, 100*3飲料）
        try:
            delta, memo = parse_expr_and_memo(text)
        except Exception:
            # 使用者主動要說明才回；或查餘額
            if text in HELP_KEYWORDS:
                help_text = (
                    "📒 個人記帳：\n"
                    "直接輸入金額或算式＋備註即可，例如：\n"
                    "+100午餐\n-30交通\n100*3飲料\n\n"
                    "查餘額：餘額 或 balance\n"
                    "/id：查看你的 userId\n\n"
                    "👭 若要群組記帳，把我拉進群組再照『姐姐 / 小朋友』說明操作。"
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
            elif text in ["餘額", "balance"]:
                bal = round(user_balances[uid], 2)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"目前餘額：{bal} 台幣"))
            # 其他文字就忽略
            return

        prev_bal = user_balances[uid]
        user_balances[uid] += delta
        new_bal = user_balances[uid]

        # 個人模式：目前金額就當「目前餘額」
        flex = build_settle_flex(
            prev_amount=prev_bal,
            delta=delta,
            total=new_bal,
            unit="台幣",
            current_label="目前餘額",
            memo=memo
        )
        line_bot_api.reply_message(event.reply_token, flex)
        return


if __name__ == "__main__":
    # 本機測試用；在 Render 上會用 gunicorn 啟動
    app.run(host="0.0.0.0", port=8000)


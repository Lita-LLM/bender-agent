"""73 обычные реакции по Telegram Bot API 10.3 (сверено 24.10.2026)."""
from ..core.errors import HumanError

# Список из сохранённой справки, сравнение по кодпоинтам; U+FE0F в API нет.
EMOJI = tuple('❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡'.split())


def emoji(value):
    value=value.replace('\ufe0f','')
    if value not in EMOJI:
        raise HumanError('reaction','Такой реакции Telegram не принимает','Выбери одну: '+' '.join(EMOJI))
    return value

"""离线 mock 提供方 —— 不联网、不需要密钥，把整条流水线跑通。

作用有两个：

1. **自测**：``tools/tests/test_autoLCDE.py`` 用它做端到端测试（含校验失败→回喂重写的闭环）。
2. **试用**：在花钱买 token 之前，先看看生成器会产出什么样的剧本、
   目录里会落哪些文件（``python tools/autoLCDE.py gen --provider mock ...``）。

内置的是一段**原创**短剧《末班雪》，六个场次，覆盖了全部可用演出手段
（进入/退出/移动/换表情/前置压暗/震动/淡入淡出/字幕角色/BGM/SFX/停顿）。

它同样遵守真实模型的调用约定：``chat(messages, stage=..., meta=...)``。
``meta`` 里带的是结构化上下文（场次下标、概念表），真实客户端会忽略它，
mock 用它决定该回哪一场。

故障注入：``AUTOLCDE_MOCK_FLAWS=1``（或 ``--mock-flaws 1``）会让第一场里多出一条
**故意非法**的 ``raw`` 命令，用来触发 validate 报错，从而验证「错误回喂 → 重写」
这条路是通的。默认 0，即产出必然合法。
"""

from __future__ import annotations

import copy
import json

from .llm import Usage, estimate_tokens

__all__ = ["MockClient", "PLAY"]

TITLE = "末班雪"
LOGLINE = ("四等小站的末班车十年前就停运了，可每个雪夜都有人来等；"
           "老周守着空站台，也守着一份没人来取的旧值班记录。")
TONE = "安静的冬日小品，克制、留白，结尾克制地暖一下"

CAST = [
    {
        "id": "老周", "name": "老周", "camp": "四等小站·值班员",
        "nameColor": "E8D8B0", "campColor": "8A7440",
        "faces": ["常态", "笑", "皱眉", "叹气"],
        "notes": "五十多岁，话少，动作慢；围着旧棉大衣，手里总有个搪瓷缸。",
    },
    {
        "id": "苏晚", "name": "苏晚", "camp": "旅客",
        "nameColor": "AFC7E0", "campColor": "5A6E80",
        "faces": ["平静", "疑惑", "微笑", "低头"],
        "notes": "三十岁上下，风衣上落着雪；一直在看时刻表，说话很轻。",
    },
    {
        "id": "广播", "name": "广播", "camp": "车站广播",
        "nameColor": "C8CDD4", "campColor": "6A7078",
        "subtitles": True,
        "faces": ["电波"],
        "notes": "字幕型角色，只出名字框，从不上屏。",
    },
    {
        "id": "巡道工", "name": "巡道工", "camp": "工务段",
        "nameColor": "C8C8C8", "campColor": "6A6A6A",
        "faces": ["风雪"],
        "notes": "龙套，两句词，拎着信号灯。",
    },
]

BACKGROUNDS = [
    {"name": "候车厅·雪夜.png", "label": "四等小站 · 候车厅"},
    {"name": "月台·雪.png", "label": "四等小站 · 月台"},
    {"name": "铁路道口·夜.png", "label": "铁路道口 · 夜"},
]

AUDIO = {
    "bgm": [
        {"key": "hall", "path": "event:/BGM/hall_night", "usage": "候车厅，安静的主旋律"},
        {"key": "snow", "path": "event:/BGM/snow_theme", "usage": "户外雪夜，单薄的弦乐"},
        {"key": "ending", "path": "event:/BGM/ending", "usage": "结尾，克制地暖一下"},
    ],
    "sfx": [
        {"key": "wind", "path": "event:/SFX/wind", "usage": "风雪环境音"},
        {"key": "bell", "path": "event:/SFX/station_bell", "usage": "站铃"},
        {"key": "kettle", "path": "event:/SFX/kettle", "usage": "水壶烧开"},
        {"key": "door", "path": "event:/SFX/door", "usage": "推拉门"},
        {"key": "steps", "path": "event:/SFX/steps_snow", "usage": "踩雪"},
        {"key": "train", "path": "event:/SFX/train_pass", "usage": "远处车过"},
        {"key": "whistle", "path": "event:/SFX/whistle", "usage": "汽笛"},
        {"key": "paper", "path": "event:/SFX/paper", "usage": "翻纸"},
        {"key": "light", "path": "event:/SFX/light_burst", "usage": "灯光明灭"},
    ],
}


def _scenes():
    """六个场次：outline 字段 + lines。"""
    return [
        {
            "index": 1, "title": "候车厅·末班", "background": "候车厅·雪夜.png",
            "label": "四等小站 · 候车厅", "bgm": "hall",
            "summary": "雪夜，老周烧水值夜。苏晚推门进来，问末班车。广播播报：本线末班车已停运十年。",
            "beats": ["老周烧水，站里只有一盏灯", "苏晚进门抖雪，径直看时刻表", "广播播报停运", "苏晚说她在等人，老周没接话"],
            "notes": ["空旷长镜头：一盏灯、一个搪瓷缸、一整面墙的时刻表"],
            "lines": [
                {"kind": "narr", "text": "候车厅只有一盏顶灯亮着。长椅空着，时刻表还在，纸已经黄了。"},
                {"kind": "narr", "text": "老周把搪瓷缸放在铁皮炉上，听水响。"},
                {"kind": "sfx", "cue": "kettle"},
                {"kind": "shot", "text": "全景：空旷候车厅，灯只亮一盏；缓推至铁皮炉。"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "center"},
                {"kind": "sfx", "cue": "wind"},
                {"kind": "narr", "text": "风把门顶开一条缝。一个女人带着一身雪进来。"},
                {"kind": "sfx", "cue": "door"},
                {"kind": "face", "who": "老周", "face": "皱眉"},
                {"kind": "enter", "who": "苏晚", "face": "平静", "slot": "right"},
                {"kind": "move", "who": "老周", "slot": "left"},
                {"kind": "say", "who": "苏晚", "text": "末班车。几点。"},
                {"kind": "say", "who": "老周", "text": "……几点都一样。这站没有末班车。"},
                {"kind": "say", "who": "苏晚", "text": "时刻表上写着，二十三点四十。"},
                {"kind": "face", "who": "老周", "face": "叹气"},
                {"kind": "say", "who": "老周", "text": "写着。挂了十年了。"},
                {"kind": "sfx", "cue": "light"},
                {"kind": "board", "text": "播报：K7xx 次，本线末班，已于十年前的今日停运。请旅客自行安排。"},
                {"kind": "say", "who": "苏晚", "face": "疑惑", "text": "……十年前的今日。"},
                {"kind": "say", "who": "老周", "text": "每年的今日都有人来。你们记性真好。"},
                {"kind": "say", "who": "苏晚", "face": "低头", "text": "我不是记性好。我在等人。"},
                {"kind": "narr", "text": "老周没接话，把水从炉上端下来，倒进两个缸子。"},
                {"kind": "pause", "seconds": 0.8},
                {"kind": "say", "who": "老周", "face": "常态", "text": "外面冷。进来坐。"},
                {"kind": "wait"},
            ],
        },
        {
            "index": 2, "title": "月台·道岔", "background": "月台·雪.png",
            "label": "四等小站 · 月台", "bgm": "snow",
            "summary": "两人上到月台看轨面。巡道工来报道岔冻住。苏晚说，她等的人十年前就在这条线上。",
            "beats": ["雪落在铁轨上，轨面亮", "巡道工报道岔冻住", "苏晚说她等的人在这条线上", "老周问了半句又咽回去"],
            "notes": ["俯拍铁轨：雪落进轨腰；巡道工信号灯的红光扫过"],
            "lines": [
                {"kind": "sfx", "cue": "steps"},
                {"kind": "narr", "text": "月台的雪没扫。铁轨埋在雪里，只在中间亮出两道细光。"},
                {"kind": "enter", "who": "苏晚", "face": "平静", "slot": "center"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "left"},
                {"kind": "move", "who": "苏晚", "slot": "right"},
                {"kind": "shot", "text": "俯拍：雪落进轨腰，两道铁光延伸出画。"},
                {"kind": "sfx", "cue": "light"},
                {"kind": "enter", "who": "巡道工", "face": "风雪", "slot": "center"},
                {"kind": "say", "who": "巡道工", "text": "周师傅！三号道岔冻住了，撬杠撬不动。"},
                {"kind": "say", "who": "老周", "face": "皱眉", "text": "冻住就冻着。今晚没有车。"},
                {"kind": "say", "who": "巡道工", "text": "……行。那我回去烤火。"},
                {"kind": "exit", "who": "巡道工"},
                {"kind": "say", "who": "苏晚", "text": "他刚才跑得挺急。"},
                {"kind": "say", "who": "老周", "text": "他说了三十二年「今晚没有车」。习惯了。"},
                {"kind": "pause", "seconds": 0.6},
                {"kind": "say", "who": "苏晚", "face": "低头", "text": "十年前的今晚，这趟车从这儿过。"},
                {"kind": "say", "who": "苏晚", "text": "他在车上。"},
                {"kind": "say", "who": "老周", "face": "常态", "text": "……"},
                {"kind": "say", "who": "老周", "text": "哪一趟。"},
                {"kind": "say", "who": "苏晚", "text": "我不说。"},
                {"kind": "shake", "who": "苏晚", "strength": 6},
                {"kind": "sfx", "cue": "wind"},
                {"kind": "narr", "text": "雪忽然密了一阵，把轨面重新盖住。"},
                {"kind": "wait"},
            ],
        },
        {
            "index": 3, "title": "候车厅·热水",
            "background": "候车厅·雪夜.png",
            "label": "四等小站 · 候车厅", "bgm": "hall",
            "summary": "回到候车厅。老周给她倒热水。苏晚说她其实知道那人不会来；老周说那他陪她等到雪停。",
            "beats": ["两缸热水，一人一个", "苏晚承认她知道结果", "老周不问理由", "老周说陪她等到雪停"],
            "notes": ["近景：两缸热水冒着白气；对话全部压在中景"],
            "lines": [
                {"kind": "sfx", "cue": "door"},
                {"kind": "narr", "text": "两人回到厅里，炉子上还剩一点热。"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "left"},
                {"kind": "enter", "who": "苏晚", "face": "平静", "slot": "right"},
                {"kind": "shot", "text": "特写：两只搪瓷缸，白气慢慢升起来。"},
                {"kind": "say", "who": "老周", "face": "笑", "text": "喝点。站里的水，不收钱。"},
                {"kind": "say", "who": "苏晚", "face": "微笑", "text": "谢谢。"},
                {"kind": "pause", "seconds": 0.5},
                {"kind": "say", "who": "苏晚", "face": "低头", "text": "我知道他不会来。"},
                {"kind": "say", "who": "老周", "text": "嗯。"},
                {"kind": "say", "who": "苏晚", "text": "……你不问为什么？"},
                {"kind": "say", "who": "老周", "face": "常态", "text": "问了也一样。今晚雪停之前，你都在等。"},
                {"kind": "front", "who": "老周", "hide": True},
                {"kind": "say", "who": "老周", "text": "我陪着。反正值班室也只有我一个人。"},
                {"kind": "undim", "who": "苏晚"},
                {"kind": "say", "who": "苏晚", "face": "微笑", "text": "……好。"},
                {"kind": "wait"},
            ],
        },
        {
            "index": 4, "title": "道口·信号", "background": "铁路道口·夜.png",
            "label": "铁路道口 · 夜", "bgm": "snow",
            "summary": "两人走到道口，看信号灯。远处有车过，苏晚说她听见了汽笛。",
            "beats": ["道口的灯在雪里明灭", "广播循环提示", "远处车声", "苏晚说她听见了"],
            "notes": ["道口横杆的影子横过画面；车声只给声音不给画面"],
            "lines": [
                {"kind": "sfx", "cue": "steps"},
                {"kind": "narr", "text": "道口的灯一下一下地亮，把雪照成一格一格的。"},
                {"kind": "enter", "who": "苏晚", "face": "平静", "slot": "right"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "left"},
                {"kind": "sfx", "cue": "light"},
                {"kind": "board", "text": "播报：前方道口，注意来车。", },
                {"kind": "sfx", "cue": "train"},
                {"kind": "narr", "text": "很远的地方，有车轮压过铁轨的声音，一阵就没了。"},
                {"kind": "say", "who": "苏晚", "face": "疑惑", "text": "……你听见了吗。"},
                {"kind": "say", "who": "老周", "text": "听见了。风从山口过来，像车。"},
                {"kind": "say", "who": "苏晚", "face": "微笑", "text": "就是车。"},
                {"kind": "say", "who": "老周", "face": "笑", "text": "……行。就是车。"},
                {"kind": "sfx", "cue": "whistle"},
                {"kind": "pause", "seconds": 0.7},
                {"kind": "narr", "text": "雪小了一点。"},
                {"kind": "wait"},
            ],
        },
        {
            "index": 5, "title": "候车厅·值班本", "background": "候车厅·雪夜.png",
            "label": "四等小站 · 候车厅", "bgm": "hall",
            "summary": "老周翻出十年前的旧值班本，找到那一夜的记录，撕下来给她。",
            "beats": ["老周从抽屉里拿出旧本子", "翻到十年前的今日", "记录上写着一句「有一名旅客未下车」", "老周把那一页撕下来给她"],
            "notes": ["特写：发黄的纸、蓝黑墨水；撕纸的声音要清楚"],
            "lines": [
                {"kind": "sfx", "cue": "paper"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "center"},
                {"kind": "narr", "text": "老周从值班室抽屉里拿出一个牛皮纸封面的本子，边角卷了。"},
                {"kind": "say", "who": "老周", "text": "十年前的记录，我都留着。"},
                {"kind": "shot", "text": "特写：发黄的纸页，蓝黑墨水，日期栏写着十年前的今日。"},
                {"kind": "say", "who": "老周", "face": "皱眉", "text": "这一页……写着「有一名旅客未下车」。"},
                {"kind": "sfx", "cue": "light"},
                {"kind": "narr", "text": "厅里安静下来。炉子上最后一点水声也停了。"},
                {"kind": "front", "who": "苏晚", "hide": True},
                {"kind": "say", "who": "苏晚", "face": "低头", "text": "……是我没让他下。"},
                {"kind": "undim", "who": "老周"},
                {"kind": "say", "who": "老周", "text": "记录里不写这个。"},
                {"kind": "say", "who": "老周", "face": "叹气", "text": "记录里只写，那晚的雪，比今晚大。"},
                {"kind": "sfx", "cue": "paper"},
                {"kind": "narr", "text": "他把那一页沿着装订线撕下来，折了两折，递过去。"},
                {"kind": "say", "who": "苏晚", "face": "微笑", "text": "……这不合规矩。"},
                {"kind": "say", "who": "老周", "face": "笑", "text": "这站十年前就没规矩了。"},
                {"kind": "wait"},
            ],
        },
        {
            "index": 6, "title": "月台·雪停", "background": "月台·雪.png",
            "label": "四等小站 · 月台", "bgm": "ending",
            "summary": "雪停。苏晚道谢，留下围巾走了。老周回到值班室，广播照旧播报，天要亮了。",
            "beats": ["雪停，天边泛白", "苏晚把围巾留在长椅上", "老周独自回到值班室", "广播照旧，字幕收尾"],
            "notes": ["结尾长镜头：空月台、天光、一盏还亮着的灯"],
            "lines": [
                {"kind": "sfx", "cue": "wind"},
                {"kind": "enter", "who": "苏晚", "face": "平静", "slot": "right"},
                {"kind": "enter", "who": "老周", "face": "常态", "slot": "left"},
                {"kind": "narr", "text": "雪停了。东边的天有一点灰白。"},
                {"kind": "say", "who": "苏晚", "face": "微笑", "text": "谢谢你的水，还有那一页纸。"},
                {"kind": "say", "who": "老周", "text": "路上小心。"},
                {"kind": "say", "who": "苏晚", "text": "……明年今日，要是还下雪，我再来。"},
                {"kind": "say", "who": "老周", "face": "笑", "text": "来。炉子一直有热水。"},
                {"kind": "exit", "who": "苏晚"},
                {"kind": "sfx", "cue": "steps"},
                {"kind": "narr", "text": "长椅上留下一条围巾。老周捡起来，搭在胳膊上。"},
                {"kind": "move", "who": "老周", "slot": "center"},
                {"kind": "bgm", "cue": None},
                {"kind": "sfx", "cue": "light"},
                {"kind": "board", "text": "播报：今日首班，五时二十。祝旅客一路平安。"},
                {"kind": "say", "who": "老周", "face": "常态", "text": "……首班。"},
                {"kind": "pause", "seconds": 1.0},
                {"kind": "narr", "text": "他把围巾叠好放进抽屉，关灯，转身锁门。"},
                {"kind": "shot", "text": "结尾长镜头：空月台、天光、一盏还亮着的灯。"},
                {"kind": "exit", "who": "老周"},
                {"kind": "wait"},
            ],
        },
    ]


PLAY = {
    "title": TITLE,
    "logline": LOGLINE,
    "tone": TONE,
    "cast": CAST,
    "backgrounds": BACKGROUNDS,
    "audio": AUDIO,
    "scenes": _scenes(),
}


class MockClient:
    """接口与 :class:`autoLCDE.llm.ChatClient` 一致，但不发网络请求。"""

    def __init__(self, settings=None, log=None, flaws: int = 0) -> None:
        self.settings = settings
        self.log = log or (lambda *_a, **_k: None)
        self.usage = Usage()
        self.flaws = int(flaws or 0)
        self.calls: list[str] = []

    # -- 接口 -------------------------------------------------------------- #
    def chat(self, messages, *, stage: str = "", temperature=None,
             max_tokens=None, json_object: bool = True, meta=None) -> str:
        meta = dict(meta or {})
        indexes = meta.get("scene_indices")
        if not isinstance(indexes, (list, tuple)) or not indexes:
            single = meta.get("scene_index")
            if single is not None:
                indexes = [single]
            else:
                # 没有 meta（例如把 mock 当成假服务端放在 HTTP 后面）就从提示词里认序号
                indexes = _guess_scene_indexes(messages)
        indexes = [int(index) for index in indexes]
        total = int(meta.get("scene_total") or 0)

        if stage == "concept":
            payload = self._concept(int(meta.get("scenes") or 6))
        elif stage == "scene":
            scenes = [self._scene(index, total, flawed=True) for index in indexes]
            payload = {"scene": scenes[0]} if len(scenes) == 1 else {"scenes": scenes}
        elif stage == "repair":
            # 真实模型在「回喂重写」时会照着报错改；mock 也照做：不再注入缺陷。
            payload = {"scene": self._scene(indexes[0], total, flawed=False)}
        else:
            payload = {"ok": True, "stage": stage}

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.calls.append(stage)
        prompt = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        completion = estimate_tokens(text)
        self.usage.add(stage, prompt, completion, estimated=True)
        self.log("debug", "[mock:%s] 返回 %d 字" % (stage or "chat", len(text)))
        return text

    def ping(self) -> str:
        return "可用（mock）"

    # -- 内容 -------------------------------------------------------------- #
    def _concept(self, scenes: int) -> dict:
        outline = []
        for position in range(max(1, scenes)):
            scene = PLAY["scenes"][position % len(PLAY["scenes"])]
            outline.append({
                "index": position + 1,
                "title": scene["title"],
                "background": scene["background"],
                "label": scene["label"],
                "bgm": scene["bgm"],
                "summary": scene["summary"],
                "beats": scene["beats"],
                "notes": scene["notes"],
            })
        return {
            "title": PLAY["title"],
            "logline": PLAY["logline"],
            "tone": PLAY["tone"],
            "cast": copy.deepcopy(PLAY["cast"]),
            "backgrounds": copy.deepcopy(PLAY["backgrounds"]),
            "audio": copy.deepcopy(PLAY["audio"]),
            "scenes": outline,
        }

    def _scene(self, index: int, total: int | None, *, flawed: bool = False) -> dict:
        """返回**单个场景对象**（不含 ``{"scene": ...}`` 外壳）。"""
        scene = copy.deepcopy(PLAY["scenes"][int(index) % len(PLAY["scenes"])])
        lines = list(scene["lines"])
        if flawed and self.flaws and int(index) == 0:
            # 故意写一条非法的 raw 命令（屏幕上还没有立绘就 COut portrait=9），
            # 用来验证「validate 报错 → 回喂模型重写」这条路。
            lines = lines[:6] + [
                {"kind": "raw", "command": {"type": "COut", "portrait": 9, "time": 0.3}},
            ] + lines[6:]
        return {
            "index": (int(index) + 1) if total else scene["index"],
            "title": scene["title"],
            "background": scene["background"],
            "label": scene["label"],
            "bgm": scene["bgm"],
            "summary": scene["summary"],
            "notes": scene["notes"],
            "lines": lines,
        }


def _guess_scene_indexes(messages) -> list[int]:
    """``meta`` 没给时，从提示词里认 ``"场景序号": n``（提示词里会出现一次或多次）。"""
    import re
    blob = "\n".join(str(m.get("content", "")) for m in (messages or [])
                     if isinstance(m, dict))
    found = re.findall(r'"场景序号"\s*:\s*(\d+)', blob)
    if found:
        return [max(0, int(value) - 1) for value in found]
    found = re.findall(r'"scene_index"\s*:\s*(\d+)', blob)
    return [int(value) for value in found] or [0]

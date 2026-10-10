"""qsim 网页模板加载。

样式与脚本已拆分为静态文件（qsim/services/web/static/app.css / app.js），
HTML 结构在 templates/dashboard.html；占位符 {status}{ts}{ov_cards}
{net_cards}{bt_cards}{trd_cards}{ai_cards} 由 mobile.render_page 用
str.format 填充。
"""

from pathlib import Path

PAGE = (Path(__file__).resolve().parent / "templates" / "dashboard.html").read_text(encoding="utf-8")

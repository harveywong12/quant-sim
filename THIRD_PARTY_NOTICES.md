# Third-party static resources

The web runtime serves these resources locally. Original upstream copyright headers and license texts are retained. No new framework dependency was introduced by this source publication.

| Component | Bundled version | Upstream | Terms / bundled license |
|---|---|---|---|
| Apache ECharts | 5.5.0 | [apache/echarts](https://github.com/apache/echarts/tree/5.5.0) | Apache-2.0; `qsim/services/web/static/licenses/echarts-5.5.0-LICENSE.txt`, `echarts-5.5.0-NOTICE.txt` |
| ECharts d3-derived portions | as included in ECharts 5.5.0 | [upstream d3 license](https://github.com/apache/echarts/blob/5.5.0/licenses/LICENSE-d3) | BSD-3-Clause; `echarts-5.5.0-LICENSE-d3.txt` |
| Tabler core | 1.4.0 | [tabler/tabler](https://github.com/tabler/tabler/tree/%40tabler/core%401.4.0) | MIT; `tabler-1.4.0-LICENSE.txt` |

ECharts license and notice, and its referenced d3 license, were checked against the official version on 2026-10-10. Tabler license was verified against its official scoped tag on 2026-10-06 and its unchanged captured license was reused; access dates do not imply use of the newest release. Bundled asset versions are retained for compatibility.

Python dependencies are declared separately in `pyproject.toml` and the requirements files. Their code is installed as dependencies, rather than copied into this repository. This publication does not change the license of project-owned code.

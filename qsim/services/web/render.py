"""qsim 网页渲染层：从旧系统 mobile_server 移植的 A 级纯渲染函数。

这些函数只做 HTML/JS 生成，不依赖旧系统业务模块。
"""

ACCENT, DANGER, WARN = "#26A69A", "#EF5350", "#E0A72E"
BLUE = "#7B8FD4"
BG_CARD, LINE, TEXT, DIM = "#12151A", "#232830", "#E8EAED", "#8A919C"
_TRADE_T0_MS = 1577808000000
_TRADE_SPAN_MIN = 250
_INTRADAY_END_MIN = 245

def _stat_card(label: str, value: str, delta: str = "", delta_cls: str = "",
               sub: str = "") -> str:
    """指标卡：标签 + 大数字 + 涨跌 + 副注（col-3，栅格自适应）。"""
    d = f'<div class="stat-delta {delta_cls}">{delta}</div>' if delta else ""
    s = f'<div class="stat-sub">{sub}</div>' if sub else ""
    return (f'<div class="col-6 col-xl-3">'
            f'<div class="card q-stat h-100"><div class="card-body">'
            f'<div class="stat-title">{label}</div>'
            f'<div class="stat-value">{value}</div>{d}{s}'
            f'</div></div></div>')


def _tcard(title: str, body: str, actions: str = "", cls: str = "",
           pad: bool = True, col: str = "col-12") -> str:
    """标准 Tabler 卡片：card-header(标题+动作) + card-body。col 控制栅格宽度。"""
    body_cls = "card-body" if pad else "card-body p-0"
    act = f'<span class="card-action">{actions}</span>' if actions else ""
    return (f'<div class="{col}"><div class="card {cls} h-100">'
            f'<div class="card-header"><span class="card-title">{title}</span>{act}</div>'
            f'<div class="{body_cls}">{body}</div></div></div>')


def _data_table(headers: list, rows: list, right: tuple = (), cls: str = "",
                details: dict | None = None, detail_ids: dict | None = None) -> str:
    """Tabler 风格数据表。right 指定右对齐列下标；details={行号: 详情HTML} 在对应行下插入可展开子行。"""
    th = "".join(f'<th class="text-end">{h}</th>' if i in right
                 else f"<th>{h}</th>" for i, h in enumerate(headers))
    trs = ""
    for i, r in enumerate(rows):
        tds = "".join(
            f'<td class="text-end" data-label="{_html_safe(headers[i])}">{c}</td>'
            if i in right else f'<td data-label="{_html_safe(headers[i])}">{c}</td>'
            for i, c in enumerate(r))
        trs += f"<tr>{tds}</tr>"
        if details and i in details:
            identity = (f' id="{_html_safe(detail_ids[i])}" data-qsim-account-detail="1"'
                        if detail_ids and i in detail_ids else "")
            trs += (f'<tr class="q-detail"><td colspan="{len(headers)}">'
                    f'<details class="q-detail-box"{identity}>{details[i]}</details></td></tr>')
    return (f'<div class="table-scroll"><table class="table q-table table-hover mb-0 {cls}">'
            f'<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>')


def _html_safe(v) -> str:
    import html as _h
    return _h.escape(str(v))


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def _day_of(s) -> str:
    """快照日期(20260805)与盘中时间戳(2026-08-12 11:00)统一为 YYYYMMDD。"""
    s = str(s)
    return s[:8] if len(s) == 8 else s[:10].replace("-", "")


def _echarts_div(div_id: str, series: list[dict], height: int = 220,
                 ytitle: str = "", default_today: bool = False,
                 show_btns: bool = True, x_range=None, days=None) -> str:
    """ECharts 交互图容器：图表内双指捏合缩放 + 拖动平移（dataZoom.inside），无底部滑块条。

    series: [{x: [...毫秒时间戳], y: [...], name, color}]
    y 为收益率小数（0.004 → 0.4%），tooltip/轴均按百分比显示。
    default_today: True 时初始化即缩放到最后一个交易日的盘中点（实盘曲线用）。
    show_btns: False 时不渲染时间范围按钮（回测图用，刻度由数据跨度决定）。
    返回 <div id> + 内联 JSON 数据 + 初始化脚本（data 嵌入页面，无外网依赖）。
    """
    import json
    if x_range:
        lo, hi = x_range
    else:
        xs = [v for s in series for v in s["x"] if v is not None]
        lo, hi = (min(xs), max(xs)) if len(xs) >= 2 else (0, 1)
    # 虚拟交易日轴（series 带 real 且与 x 不同）vs 真实时间轴（回测）
    virtual = "real" in series[0] and series[0]["real"] != series[0]["x"]
    if virtual:
        interval = max(3600000, (hi - lo) // 24)  # 约 24 格
        label_rotate = 0
    else:
        interval = _pick_interval(lo, hi)
        # ECharts time 轴标签模板：{yyyy}-{MM}-{dd}（非 strftime 的 %Y-%m-%d）
        long_fmt = (hi - lo) > 180 * 86400000
        label_rotate = 45 if long_fmt else 0
    # 长标签（含年份）倾斜 45°，短标签保持水平，避免重叠
    date_fmt = "{yyyy}-{MM}-{dd}" if not virtual and (hi - lo) > 180 * 86400000 else "{MM}-{dd}"

    datasets = []
    for s in series:
        dash = "dashed" if s.get("dash") else "solid"
        datasets.append({
            "name": s.get("name", ""),
            "type": "line", "smooth": True, "symbol": "none",
            "connectNulls": True,
            "lineStyle": {"width": 2, "color": s.get("color", ACCENT),
                          "type": dash},
            "itemStyle": {"color": s.get("color", ACCENT)},
            "areaStyle": {"opacity": 0.08 if len(series) == 1 else 0},
            "data": [[x, y] for x, y in zip(s["x"], s["y"])],
        })
    # 真实时间戳数组（虚拟轴还原用）：tooltip/轴标签按 dataIndex 取真实时间。
    # 无 real 字段（回测）时退化为 x 本身，行为不变。
    _real0 = json.dumps(series[0].get("real") or series[0]["x"])
    option = {
        "backgroundColor": "transparent",
        "animation": False,
        # 倾斜标签需要更大的底部空间，避免被裁切
        "grid": {"left": 42, "right": 8, "top": 26,
                 "bottom": 52 if label_rotate else 22},
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": "#1C2333", "borderColor": "#2A3243",
            "textStyle": {"color": TEXT, "fontSize": 11},
            "formatter": None,
        },
        "xAxis": {
            "type": "time",
            "interval": interval,
            "min": lo - (hi - lo) * 0.03,
            "max": hi + (hi - lo) * 0.03,
            "axisLine": {"lineStyle": {"color": "#2A3243"}},
            "axisLabel": {"color": DIM, "fontSize": 10, "formatter": date_fmt,
                          "rotate": label_rotate, "margin": 10},
            "splitLine": {"show": False},
        },        "yAxis": {
            "type": "value",
            "axisLabel": {"color": DIM, "fontSize": 10, "formatter": None},
            "splitLine": {"lineStyle": {"color": "rgba(42,50,67,0.4)"}},
            "name": ytitle, "nameTextStyle": {"color": DIM, "fontSize": 10},
        },
        # preventDefaultMouseMove=False：ECharts 默认会拦截触摸滚动（内部调用
        # preventDefault），显式关闭后垂直滑动手势归还页面滚动；水平/捏合缩放仍由图表处理
        "dataZoom": [
            {"type": "inside", "xAxisIndex": 0,
             "zoomOnMouseWheel": True, "moveOnMouseMove": False,
             "preventDefaultMouseMove": False},
        ],
        "legend": {
            "show": len(series) > 1 and bool(series[0].get("name")),
            "textStyle": {"color": DIM, "fontSize": 10},
            "top": 0, "left": 4, "itemWidth": 14, "itemHeight": 2,
        },
        "series": datasets,
    }
    # formatter 必须是真实 JS 函数（ECharts 对字符串当模板原样输出，不会执行）
    # 故 JSON 中置 None，渲染前注入函数
    yfmt = ("function(v){var p=v*100;"
            "return p.toFixed(2).replace(/\\.?0+$/,'')+'%';}")
    tfmt = ("function(params){"
            "var t=params[0].value[0];"
            "var d=ISV?new Date(_restore(t)):new Date(t);"
            "var ds=d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2)"
            "+' '+'('+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)+')';"
            "var rows=params.map(function(p){return p.marker+' '+p.seriesName+': <b>'+"
            "(p.value[1]*100).toFixed(2)+'%</b>';});return ds+'<br>'+rows.join('<br>');}")
    # ECharts 触控手势需要容器 CSS 配合：pan-y 让垂直滑动手势留给页面滚动，
    # 水平捏合/平移由 ECharts dataZoom.inside 处理，二者互不抢事件。
    # 双指捏合缩放由下方 JS 在捕获阶段接管（衰减灵敏度 SENS），
    # 同时 stopImmediatePropagation 阻止 ECharts 内置 pinch 双重缩放；
    # 桌面滚轮缩放仍由 ECharts dataZoom 处理。
    pinch_js = ("""
      (function(){
        var el=document.getElementById('%s'),chart=echarts.getInstanceByDom(el);
        if(!chart)return;
        var dz=null,dist0=0,active=false;
        var SENS=0.45;  // 灵敏度衰减系数：<1 时捏合缩放更平缓（可调）
        var grab=function(e){          if(e.touches.length===2){
            dz=chart.getOption().dataZoom[0];
            dist0=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                             e.touches[0].clientY-e.touches[1].clientY);
            active=true;
            if(e.cancelable)e.preventDefault();
            e.stopImmediatePropagation();
          }else if(e.touches.length<2&&active){
            active=false;
          }
        };
        var pinch=function(e){
          if(!active||e.touches.length!==2)return;
          var d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                           e.touches[0].clientY-e.touches[1].clientY);
          if(dist0>0){
            // 距离比 r=d/dist0；捏合张开(r>1)放大图表 → span 缩小
            // SENS 衰减缩放幅度：r=4、SENS=0.45 → span/1.8（而非无衰减的 span/4）
            var r=d/dist0;
            var factor=1+Math.abs(r-1)*SENS;
            var span=(dz.end-dz.start),mid=(dz.start+dz.end)/2;
            var newSpan=span/(r>1?factor:(2-factor));
            var ns=Math.max(0,mid-newSpan/2);
            var ne=Math.min(100,mid+newSpan/2);
            if(ne-ns>=1){
              chart.dispatchAction({type:'dataZoom',start:ns,end:ne,dataZoomIndex:0});
              dz.start=ns;dz.end=ne;
            }
            dist0=d;
          }
          if(e.cancelable)e.preventDefault();
          e.stopImmediatePropagation();
        };
        el.addEventListener('touchstart',grab,{capture:true,passive:false});
        el.addEventListener('touchmove',pinch,{capture:true,passive:false});
        el.addEventListener('touchend',function(e){
          if(e.touches.length<2)active=false;
        },{capture:true,passive:true});
      })();
    """) % div_id
    # 时间范围按钮：今日/3日/1月/全部。
    # 点击时 dataZoom 缩放 + 动态切换 x 轴刻度粒度（今日→小时、3日→半天、1月→周、全部→按跨度）。
    # 今日为压缩轴（午休 85 分钟被压缩掉）：范围 9:30 ~ 13:35（=压缩后 15:00），
    # 标签用 _restore 还原真实时间显示 9:30-11:30 / 13:00-15:00 两段。
    # 用 % 模板避免 f-string 花括号转义错误。
    lo_ms = option["xAxis"]["min"]
    hi_ms = option["xAxis"]["max"]
    btn_js = """
(function(){
  // 每按钮刻度间隔：今日1h、3日≈1交易日、1月≈5交易日、全部≈25交易日
  var btns=[["今日",0,3600000,_hms,0],["3日",3,SPAN_MS,_md,0],
            ["1月",22,5*SPAN_MS,_md,0],["全部",0,25*SPAN_MS,_y,45]];
  var LO=LOMS,HI=HIMS;
  var stateKey="CURVEKEY";
  for(var i=0;i<btns.length;i++){
    (function(lb,ndays,iv,fmt,rot){
      var b=document.createElement("button");
      b.type="button";
      b.className="chart-range-btn";
      b.textContent=lb;
      b.addEventListener("click",function(){
        var s=LO,e=HI;
        var dts=chart.getOption().series[0].data.map(function(d){return d[0];});
        var last=dts[dts.length-1];
        if(ISV){
          var dayI=Math.floor((last-T0_MS)/SPAN_MS);
          if(lb==="今日"){
            s=Math.max(LO,T0_MS+dayI*SPAN_MS); e=Math.min(HI,T0_MS+dayI*SPAN_MS+END_MS);
          } else if(ndays>0){
            if(ndays<=5){
              var cnt={};
              for(var k=0;k<dts.length;k++){
                var di=Math.floor((dts[k]-T0_MS)/SPAN_MS);
                cnt[di]=(cnt[di]||0)+1;
              }
              var dense=Object.keys(cnt).filter(function(d){return cnt[d]>=5;}).map(Number).sort(function(a,b){return a-b;});
              var startDay= dense.length>=ndays? dense[dense.length-ndays] : (dense.length? dense[0] : Math.max(0,dayI-ndays+1));
              s=Math.max(LO,T0_MS+startDay*SPAN_MS);
            } else {
              s=Math.max(LO,T0_MS+Math.max(0,dayI-ndays+1)*SPAN_MS);
            }
            e=HI;
          }
        } else {
          var DAY=86400000;
          var days=dts.map(function(t){var d=new Date(t);return new Date(d.getFullYear(),d.getMonth(),d.getDate()).getTime();});
          var uniq=[];for(var k=0;k<days.length;k++){if(uniq.indexOf(days[k])<0)uniq.push(days[k]);}
          uniq.sort(function(a,b){return a-b;});
          var lastDay=uniq[uniq.length-1];
          if(lb==="今日"){
            s=Math.max(LO,lastDay); e=Math.min(HI,lastDay+DAY);
          } else if(ndays>0){
            var n=(lb==="3日"?3:22);
            s=Math.max(LO,uniq[Math.max(0,uniq.length-n)]);
            e=HI;
          }
        }
        // 直接用 xAxis min/max 固定可视范围 + interval/minInterval 控制刻度密度，
        // 避免 dataZoom 与 interval 相互干扰导致标签过密重叠
        chart.setOption({
          xAxis:{min:s,max:e,interval:iv,minInterval:iv,
                 axisLabel:{formatter:fmt,rotate:rot}},
          dataZoom:{startValue:s,endValue:e}
        });
        var bs=el.parentNode.querySelectorAll("button[data-rng]");
        for(var j=0;j<bs.length;j++)bs[j].classList.remove("is-active");
        b.classList.add("is-active");
        try{sessionStorage.setItem(stateKey,b.getAttribute("data-rng"));}catch(e){}
      });
      b.setAttribute("data-rng", lb === "今日" ? "today" : lb);
      el.parentNode.insertBefore(b,el);
    })(btns[i][0],btns[i][1],btns[i][2],btns[i][3],btns[i][4]);
  }
})();
""".replace("LOMS", str(lo_ms)).replace("HIMS", str(hi_ms)) \
  .replace("CURVEKEY", "quant_curve_period_" + div_id) \
  .replace("DIMCOL", DIM).replace("LINECOL", LINE).replace("ACCCOL", ACCENT)
    return (f'<div id="{div_id}" class="chart-box" style="width:100%;'
            f'touch-action:pan-y;overscroll-behavior:contain;"></div>'
            f'<script>(function(){{'
            f'var el=document.getElementById("{div_id}");'
            f'var chart=echarts.init(el);'
            f'var opt={json.dumps(option)};'
            f'var REAL0={_real0};'
            f'var T0_MS={_TRADE_T0_MS},SPAN_MS={_TRADE_SPAN_MIN*60000},END_MS={_INTRADAY_END_MIN*60000};'
            f'var ISV={1 if virtual else 0};'
            f'var _realOf=function(v){{var xs=opt.series[0].data.map(function(d){{return d[0];}});'
            f'var lo=0,hi=xs.length-1;'
            f'if(v<=xs[0])return REAL0[0];'
            f'if(v>=xs[xs.length-1])return REAL0[xs.length-1];'
            f'while(hi-lo>1){{var mid=(lo+hi)>>1;if(xs[mid]<v)lo=mid;else hi=mid;}}'
            f'return (v-xs[lo]<xs[hi]-v)?REAL0[lo]:REAL0[hi];}};'
            f'var DAYS={json.dumps(days or [])};'
            f'var _restore=function(v){{if(!ISV||!DAYS.length)return _realOf(v);'
            f'var di=Math.floor((v-T0_MS)/SPAN_MS);'
            f'if(di<0||di>=DAYS.length)return _realOf(v);'
            f'var off=(v-T0_MS-di*SPAN_MS)/60000;'
            f'var rm=off<125?off+570:off+655;'
            f'var ds=DAYS[di];'
            f'return Date.UTC(+ds.slice(0,4),+ds.slice(4,6)-1,+ds.slice(6,8),0,rm)-8*3600000;}};'
            f'var _hms=function(v){{var d=new Date(_restore(v));'
            f"return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}};"
            f'var _md=function(v){{var d=new Date(_restore(v));'
            f"return ('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);}};"
            f'var _y=function(v){{return String(new Date(_restore(v)).getFullYear());}};'
            f'opt.yAxis.axisLabel.formatter={yfmt};'
            f'opt.tooltip.formatter={tfmt};'
            + ("opt.xAxis.axisLabel.formatter=_md;"
               "opt.xAxis.interval=Math.max(3600000,(opt.xAxis.max-opt.xAxis.min)/24);"
               if virtual else "")
            + f'chart.setOption(opt);'
            f'window.addEventListener("resize",function(){{chart.resize();}});'
            f'if(window.ResizeObserver){{new ResizeObserver(function(){{chart.resize();}}).observe(el);}}'
            + (f'// 默认缩放到最后交易日\n'
               f'if(opt.series&&opt.series[0]&&opt.series[0].data&&opt.series[0].data.length>1){{'
               f'var dts=opt.series[0].data.map(function(d){{return d[0];}});'
               f'var last=dts[dts.length-1];'
               f'var s,e,iv;'
               f'if(ISV){{'
               f'var t0=T0_MS,span=SPAN_MS;'
               f'var dayI=Math.floor((last-t0)/span);'
               f's=Math.max(dts[0],t0+dayI*span); e=Math.min(last+span,t0+dayI*span+END_MS);'
               f'iv=3600000;'
               f'}}else{{'
               f'var DAY=86400000;'
               f'var d0=new Date(last);var s0=new Date(d0.getFullYear(),d0.getMonth(),d0.getDate()).getTime();'
               f's=Math.max(dts[0],s0); e=Math.min(last+DAY,s0+DAY);'
               f'iv=DAY;'
               f'}}'
               f'chart.dispatchAction({{type:"dataZoom",startValue:s,endValue:e,dataZoomIndex:0}});'
               f'chart.setOption({{xAxis:{{interval:iv,minInterval:iv,'
               f'axisLabel:{{formatter:function(v){{var d=new Date(_restore(v));'
               f"return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}},"
               f'rotate:0}}}}}},{{lazyUpdate:true}});'
               f'}}'
               if default_today else '')
            + (f'{btn_js}' if show_btns else '')
            + (f'setTimeout(function(){{'
               f'var saved=null;try{{saved=sessionStorage.getItem("quant_curve_period_{div_id}");}}catch(e){{}}'
               f'var tb=el.parentNode.querySelector("button[data-rng="+(saved||"today")+"]");'
               f'if(tb)tb.click();'
               f'}},100);'
               if default_today else '')
            + f'{pinch_js}'
            f'}})();'
            f'</script>')


def _pick_interval(lo: int, hi: int) -> int:
    """按 x 跨度(ms)选主刻度间隔(ms)：窄区间用日粒度，多年数据自动放宽"""
    span = hi - lo
    DAY = 86400000
    if span <= 45 * DAY:
        return DAY                      # 1 天
    if span <= 180 * DAY:
        return 7 * DAY                  # 1 周
    if span <= 2 * 365 * DAY:
        return 30 * DAY                 # 1 月
    if span <= 5 * 365 * DAY:
        return 90 * DAY                 # 1 季
    return 365 * DAY                    # 1 年


def _dates(series) -> list[int]:
    """日期 → 本地时区毫秒时间戳（东八区）。支持 YYYYMMDD（每日快照）
    与 YYYY-MM-DD HH:MM（盘中 5 分钟点），echarts time 轴直接定位。

    盘中点做午休压缩：13:00 后的点前移 85 分钟（映射到 11:35 之后），
    使 x 轴 9:30-11:30 / 13:00-15:00 两段交易时段紧邻，
    午休不再占轴空间（TradingView 风格）。tooltip/标签用 _restore_ts 还原。
    """
    import datetime as dt
    out = []
    for d in series:
        s = str(d)
        try:
            if len(s) == 8:
                ts = dt.datetime.strptime(s, "%Y%m%d")
            else:
                ts = dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
                if ts.hour * 60 + ts.minute >= 780:  # 13:00 后：减去午休 85 分钟
                    ts = ts - dt.timedelta(minutes=85)
            out.append(int(ts.timestamp() * 1000))
        except (ValueError, TypeError):
            continue
    return out


def _restore_ts(ts_ms: int) -> int:
    """还原午休压缩：压缩后落在当日 11:35~13:35 区间的点（原 13:00~15:00）加回 85 分钟。
    用于 tooltip/x 轴标签显示真实时间；快照点（00:00）与上午点不受影响。"""
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone(dt.timedelta(hours=8)))
    day0 = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mins = (ts_ms - int(day0.timestamp() * 1000)) / 60000
    if 695 <= mins <= 815:
        return ts_ms + 85 * 60000
    return ts_ms


def _trading_axis(series, common_days=None) -> tuple[list[int], list[int], list[str]]:
    """交易日等距虚拟轴：每个交易日固定 250 分钟宽，非交易日不占空间。

    返回 (virtual_ms[], real_ms[], days[])：
      virtual_ms  绘图用 x（虚拟时间戳，T0 + day_idx*SPAN + 日内偏移）
      real_ms     真实时间戳（tooltip 还原显示）
      days        交易日 YYYYMMDD 升序（轴标签还原 day_idx → 日期）
    快照点（YYYYMMDD）映射到当日收盘位（偏移 245），盘中点映射到压缩后的日内偏移。
    common_days：多图共享的交易日基线（升序 YYYYMMDD）。传入后所有图对同一日期
    使用相同 day_idx，实盘各策略曲线 X 轴坐标才能对齐。
    """
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=8))
    parsed: list[tuple[dt.datetime, bool]] = []  # (真实时间, 是否快照)
    for d in series:
        s = str(d)
        try:
            if len(s) == 8:
                ts = dt.datetime.strptime(s, "%Y%m%d").replace(tzinfo=tz)
                parsed.append((ts, True))
            else:
                ts = dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                parsed.append((ts, False))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return [], [], []
    if common_days:
        days = [str(d) for d in common_days]
    else:
        days = sorted({t.strftime("%Y%m%d") for t, _ in parsed})
    day_idx = {d: i for i, d in enumerate(days)}
    span_ms = _TRADE_SPAN_MIN * 60000
    virtual, real = [], []
    for ts, is_snap in parsed:
        dkey = ts.strftime("%Y%m%d")
        if dkey not in day_idx:
            continue
        i = day_idx[dkey]
        mins = ts.hour * 60 + ts.minute
        if is_snap:
            off = _INTRADAY_END_MIN + 1  # 246：当日最后，避开 15:00 盘中点(245)
        elif mins >= 780:  # 13:00 后：压缩 85 分钟
            off = mins - 655
        else:
            off = mins - 570
        virtual.append(_TRADE_T0_MS + i * span_ms + off * 60000)
        if is_snap:
            real.append(int(ts.replace(hour=15, minute=0).timestamp() * 1000))
        else:
            real.append(int(ts.timestamp() * 1000))
    # 快照点(off=246)可能排在当日盘中点之后，按虚拟时间戳重排，
    # 否则 ECharts 按非单调 x 画线会折返（3日/1月视图线乱）
    pairs = sorted(zip(virtual, real))
    virtual = [p[0] for p in pairs]
    real = [p[1] for p in pairs]
    return virtual, real, days


def _curve_echarts(equity, color: str, div_id: str = "eq",
                   height: int = 220, initial: float = 100000.0,
                   default_today: bool = False, show_btns: bool = True,
                   trading_days=None, x_range=None, benchmark=None) -> str:
    """累计收益率 ECharts 曲线（按 initial 归一化，图表内捏合缩放）。

    实盘曲线（含盘中点）用交易日等距虚拟轴（周末/节假日折叠）；
    回测曲线（纯日线）保持真实时间轴。series 携带 real 真实时间戳供 tooltip 还原。
    trading_days/x_range：实盘多图共享交易日基线与 X 轴范围，保证各策略坐标对齐。
    benchmark：可选（date, total_value）DataFrame，画一条沪深300对比线（灰色虚线）。
    """
    if len(equity) < 2:
        return ""
    init = initial or 1.0
    has_intraday = any(len(str(d)) > 8 for d in equity["date"])
    # 实盘各策略共享 _live_common_axis 生成的虚拟交易日范围。某个策略
    # 没有盘中点时仍可能只有日线快照；若此时退回真实时间轴，x_range
    # 会是 2020 年的虚拟轴而数据点是 2026 年真实时间，ECharts 会把线
    # 全部裁出可视区域，表现为“实盘曲线没刷出来”。
    live_axis = trading_days is not None and x_range is not None
    # 数据不足 5 个交易日时不聚焦"今日"：曲线太短时默认显示全部（含 0% 起点），
    # 否则初始缩放到最后交易日，用户看不到曲线从 0 开始
    if default_today:
        days_n = len({str(d)[:8] for d in equity["date"]})
        if days_n < 5:
            default_today = False
    if has_intraday or live_axis:
        vx, real, _days = _trading_axis(equity["date"], trading_days)
        # x/y/real 三者按虚拟时间戳同步排序，保证与 _trading_axis 内部排序一致
        tri = sorted(zip(vx, real, equity["total_value"]))
        vx = [t[0] for t in tri]
        real = [t[1] for t in tri]
        vals = [t[2] for t in tri]
    else:
        vx = real = _dates(equity["date"])
        vals = equity["total_value"]
    chart_series = [{
        "x": vx,
        "y": [(float(v) / init - 1) for v in vals],
        "real": real,
        "color": color,
    }]
    if benchmark is not None and len(benchmark) >= 2:
        if has_intraday:
            bx, breal, _ = _trading_axis(benchmark["date"], trading_days)
        else:
            bx = breal = _dates(benchmark["date"])
        b0 = float(benchmark["total_value"].iloc[0]) or 1.0
        chart_series.append({
            "x": bx,
            "y": [float(v) / b0 - 1 for v in benchmark["total_value"]],
            "real": breal, "color": "#8B95A7", "name": "沪深300", "dash": True,
        })
    return _echarts_div(div_id, chart_series, height=height, ytitle="累计收益",
        default_today=default_today, show_btns=show_btns, x_range=x_range,
        days=_days if has_intraday else None)


def _multi_series(equity, initial: float, name: str, color: str) -> dict:
    """净值曲线 → 累计收益序列（与 _curve_echarts 同轴处理，供多线对比图）"""
    has_intraday = any(len(str(d)) > 8 for d in equity["date"])
    if has_intraday:
        vx, real, _ = _trading_axis(equity["date"])
        tri = sorted(zip(vx, real, equity["total_value"]))
        vx = [t[0] for t in tri]
        real = [t[1] for t in tri]
        vals = [t[2] for t in tri]
    else:
        vx = real = _dates(equity["date"])
        vals = equity["total_value"]
    init = initial or 1.0
    return {"x": vx, "real": real, "name": name, "color": color,
            "y": [(float(v) / init - 1) for v in vals]}


def _heatmap_color(day_pct: float) -> str:
    """将日涨跌幅映射为红/灰/绿，避免极端涨跌把色阶全部压扁。"""
    neutral = (47, 47, 47)
    target = (46, 158, 123) if day_pct >= 0 else (224, 93, 93)
    weight = min(abs(float(day_pct)) / 6.0, 1.0)
    rgb = tuple(round(a + (b - a) * weight) for a, b in zip(neutral, target))
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def _prev_total(df, prev_estimate: float | None = None) -> float:
    """当日收益基准：昨日收盘估值。

    prev_estimate：调用方用 pre_close（昨收价）重估的昨日估值
    （现金 + 持仓 × 昨收价）。昨收价来自实时行情表，每个交易日都有，
    不依赖记账任务是否成功——快照断档时当日收益依然准确。
    特殊场景：盘后已记账（最新快照日期 ≥ 今天）时账户持仓已是今日状态，
    基准应取昨日快照（iloc[-2]），避免用今日持仓 × 昨收价算错。
    快照中间断档（相邻两期跨过 ≥1 个交易日）时改用 prev_estimate，避免用
    更早的快照当“昨日基准”算出错误的当日收益。
    """
    import datetime as dt
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y%m%d")
    if len(df) >= 2:
        last, prev = str(df["date"].iloc[-1]), str(df["date"].iloc[-2])
        gap = True
        try:
            diff = (dt.datetime.strptime(last, "%Y%m%d")
                    - dt.datetime.strptime(prev, "%Y%m%d")).days
            gap = diff not in (1, 3)
        except ValueError:
            gap = True
        if last >= today and not gap:
            return float(df["total_value"].iloc[-2])
    if prev_estimate is not None:
        return prev_estimate
    return float(df["total_value"].iloc[-1])


def _risk_metrics(snaps, initial: float) -> dict:
    """由每日净值快照计算实盘风险指标（年化/最大回撤/夏普）"""
    import numpy as np
    if snaps is None or len(snaps) < 2:
        return {"annual": 0.0, "mdd": 0.0, "sharpe": 0.0}
    total = np.array([float(v) for v in snaps["total_value"]])
    init = initial or 1.0
    n = len(total)
    annual = (total[-1] / init) ** (252 / n) - 1 if n >= 21 else 0.0
    peak = np.maximum.accumulate(total)
    mdd = float(((total / peak) - 1).min())
    dret = np.diff(total) / total[:-1]
    sharpe = float(dret.mean() / dret.std() * (252 ** 0.5)) if len(dret) > 1 and dret.std() > 0 else 0.0
    return {"annual": annual, "mdd": mdd, "sharpe": sharpe}


def _merge_intraday(snaps, rt_nav, strategy: str):
    """合并每日快照 + 盘中 5 分钟实时净值点 → 净值曲线数据。

    snaps: 每日快照 DataFrame[date(YYYYMMDD), total_value]
    rt_nav: realtime_nav 表全量；只取 strategy 当天的盘中点追加到末尾
    返回新 DataFrame[date, total_value]（date 为 YYYY-MM-DD HH:MM 或 YYYYMMDD）
    """
    import pandas as pd
    rows = []
    for r in snaps.itertuples():
        rows.append({"date": str(r.date), "total_value": float(r.total_value)})
    if rt_nav is not None and len(rt_nav):
        sub = rt_nav[rt_nav["strategy"] == strategy]
        # 合并盘中点，但只保留交易时段（9:00-11:30、13:00-15:00），
        # 排除午休/收盘后的非交易点，保证 x 轴只显示实际交易时间的点
        for r in sub.itertuples():
            ts = str(r.ts)
            if len(ts) >= 16:
                hm = ts[11:16]
                hh = int(hm[:2])
                if (hh < 9 or (hh == 11 and hm > "11:30") or (hh == 12)
                        or (hh >= 15 and hm > "15:00")):
                    continue
            rows.append({"date": ts, "total_value": float(r.value)})
    df = pd.DataFrame(rows)
    if len(df) and "date" in df:
        df = df.drop_duplicates(subset=["date"])
        # 按真实时间排序（混合 YYYYMMDD 与 YYYY-MM-DD HH:MM 需解析后比较）
        df["_ts"] = _dates(df["date"])
        df = df.sort_values("_ts").drop(columns=["_ts"]).reset_index(drop=True)
    return df


def _bt_metrics_row(m: dict, extra: str = "") -> str:
    return (f'<div class="curve-metrics"><span>年化 <b>{m["annual_return"] * 100:.1f}%</b></span>'
            f'<span>回撤 <b style="color:{DANGER}">{m["max_drawdown"] * 100:.1f}%</b></span>'
            f'<span>夏普 <b>{m["sharpe"]:.2f}</b></span>'
            f'{_excess_html(m)}{_risk2_html(m)}{extra}</div>')


def _excess_html(m) -> str:
    e = m.get("excess_return")
    if e is None:
        return ""
    cls = "badge-p" if e >= 0 else "badge-n"
    return f'<span>超额 <b class="{cls}">{e * 100:+.1f}%</b></span>'


def _risk2_html(m) -> str:
    if m.get("sortino") is None:
        return ""
    return (f'<span>Sortino <b>{m["sortino"]:.2f}</b></span>'
            f'<span>Calmar <b>{m.get("calmar", 0):.2f}</b></span>'
            f'<span>成本 <b>{m.get("cost_drag", 0) * 100:.1f}%</b></span>')


def _regime_html(m) -> str:
    """回测按市场状态分组的收益展示（牛市/震荡/熊市各多少天、区间收益）。"""
    reg = m.get("regime") if isinstance(m, dict) else None
    if not reg:
        return ""
    parts = []
    for label, cn in (("bull", "牛市"), ("range", "震荡"), ("bear", "熊市")):
        s = reg.get(label)
        if s:
            parts.append(f"{cn} {s['days']}天 {s['total_return'] * 100:+.0f}%")
    if not parts:
        return ""
    return f'<div class="note" style="color:#8B95A7;">{" · ".join(parts)}</div>'


def _trade_timeline(div_id: str, trades: list[dict], min_date: str,
                    max_date: str, title: str = "交易记录") -> str:
    """iOS 闹钟风格时间滚轮：上下滑动选日期，中间高亮，下方显示当日买卖明细。

    trades: [{date: YYYYMMDD, side: BUY/SELL, name, code, price, shares, amount}]
    滚轮只列出有交易的日期（升序），松手惯性吸附到最近项；
    选中日期高亮显示当日交易明细。
    """
    import json
    by_date: dict[str, list] = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)
    # 只列有交易的日期，避免滑过大量无交易日
    days = sorted(by_date.keys())
    days_json = json.dumps(days).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    by_json = json.dumps(by_date).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    # 用普通字符串 + % 占位（避免 f-string 的 {{ }} 转义导致花括号错配）
    js_tpl = """(function(){
var days=%(days)s,by=%(by)s,el=document.getElementById("wp_%(id)s"),
items=document.getElementById("wpi_%(id)s"),body=document.getElementById("tlbody_%(id)s"),
count=document.getElementById("tlcount_%(id)s");
var ITEM=44,idx=days.length-1,off=0,sy=0,base=0,drag=false;
var MAX=-(days.length-1)*ITEM;
function fmt(d){return d.slice(0,4)+"-"+d.slice(4,6)+"-"+d.slice(6,8);}
function paint(o){
  var kids=items.children;
  for(var i=0;i<kids.length;i++){
    var k=kids[i],di=Math.abs(i*ITEM+o);
    k.className="wp-item"+(di<2?" sel":di<ITEM*1.5?" near":"");
    k.style.transform=di<2?"scale(1.08)":di<ITEM*1.5?"scale(0.95)":"scale(0.85)";
    k.style.opacity=di<2?1:di<ITEM*1.5?0.75:0.35;
  }
}
function esc(v){
  return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/\"/g,"&quot;").replace(/'/g,"&#39;");
}
function show(){
  var d=days[idx],list=by[d]||[];
  count.textContent=list.length?list.length+" 笔":"—";
  if(!list.length){body.innerHTML='<div class="tl-empty">'+fmt(d)+' 当日无操作</div>';return;}
  var groups={},order=[];
  for(var i=0;i<list.length;i++){
    var st=list[i].strat||"其他";
    if(!groups[st]){groups[st]=[];order.push(st);}
    groups[st].push(list[i]);
  }
  var html='<div class="tl-date" style="margin-bottom:4px;">'+fmt(d)+' · '+list.length+' 笔</div>';
  for(var g=0;g<order.length;g++){
    var st=order[g],tl=groups[st];
    html+='<div class="tl-strat" style="font-size:10px;color:#8B95A7;font-weight:700;margin:8px 0 2px;">'+esc(st)+' · '+tl.length+' 笔</div>';
    for(var j=0;j<tl.length;j++){
      var t=tl[j],sc=t.side==="BUY"?"tl-buy":"tl-sell",scn=t.side==="BUY"?"买":"卖";
      var tm=t.time?t.time.slice(11,16):"",w=t.why||{};
      var impact='成交金额约 '+Number(t.amount||0).toLocaleString('zh-CN',{maximumFractionDigits:0})+
        ' 元，费用 '+Number(t.fee||0).toFixed(2)+' 元。';
      var source=w.source==="recorded"?"成交时记录":"按当前规则回溯";
      html+='<div class="trade-entry"><div class="tl-row"><span><span class="tl-side '+sc+'">'+scn+'</span>'+
      esc(t.name)+' '+esc(t.code)+'</span><span>'+t.shares+'股 @'+t.price.toFixed(3)+
      (tm?'<span class="mut" style="margin-left:6px;">'+tm+'</span>':'')+'</span></div>'+
      '<details class="trade-why"><summary>'+esc(w.title||(t.side==="BUY"?"为什么买入":"为什么卖出"))+
      '<span class="why-source">'+source+'</span></summary><div class="why-box">'+
      '<div class="why-line"><span class="why-label">直接原因</span>'+esc(w.summary)+'</div>'+
      '<div class="why-line"><span class="why-label">策略规则</span>'+esc(w.rule)+'</div>'+
      (w.facts?'<div class="why-line"><span class="why-label">触发数据</span>'+esc(w.facts)+'</div>':'')+
      '<div class="why-line"><span class="why-label">信号时间</span>'+esc(w.signal)+'</div>'+
      '<div class="why-line"><span class="why-label">成交方式</span>'+esc(w.execution)+'</div>'+
      '<div class="why-line"><span class="why-label">资金影响</span>'+impact+'</div>'+
      '<div class="why-note">'+esc(w.note)+'</div></div></details></div>';
    }
  }
  body.innerHTML=html;
}
function settle(){
  idx=Math.round(-off/ITEM);
  idx=Math.max(0,Math.min(days.length-1,idx));
  off=-idx*ITEM;items.style.transition="transform .18s ease";
  items.style.transform="translateY("+off+"px)";paint(off);show();
  setTimeout(function(){items.style.transition="none";},200);
}
el.addEventListener("touchstart",function(e){
  if(e.touches.length===1){drag=true;sy=e.touches[0].clientY;base=off;items.style.transition="none";}
});
el.addEventListener("touchmove",function(e){
  if(!drag)return;var dy=e.touches[0].clientY-sy;
  off=Math.max(MAX,Math.min(0,base+dy));
  items.style.transform="translateY("+off+"px)";paint(off);
  if(e.cancelable)e.preventDefault();
},{passive:false});
el.addEventListener("touchend",function(){if(drag){drag=false;settle();}});
/* 桌面鼠标支持：点击选中 / 滚轮换日期 / 拖拽平移 */
var mdown=false,mdrag=false,my=0,mbase=0;
items.addEventListener("click",function(e){
  if(mdrag){mdrag=false;return;}
  var t=e.target;
  while(t&&t!==items&&!t.hasAttribute("data-d"))t=t.parentNode;
  if(t&&t.hasAttribute("data-d")){
    var i=days.indexOf(t.getAttribute("data-d"));
    if(i>=0){idx=i;off=-i*ITEM;settle();}
  }
});
el.addEventListener("wheel",function(e){
  e.preventDefault();
  var ni=Math.max(0,Math.min(days.length-1,idx+(e.deltaY>0?1:-1)));
  idx=ni;off=-ni*ITEM;settle();
},{passive:false});
el.addEventListener("mousedown",function(e){mdown=true;mdrag=false;my=e.clientY;mbase=off;items.style.transition="none";e.preventDefault();});
window.addEventListener("mousemove",function(e){
  if(!mdown)return;
  var dy=e.clientY-my;
  if(Math.abs(dy)>4)mdrag=true;
  off=Math.max(MAX,Math.min(0,mbase+dy));
  items.style.transform="translateY("+off+"px)";paint(off);
});
window.addEventListener("mouseup",function(){if(mdown){mdown=false;settle();}});
off=-idx*ITEM;items.style.transform="translateY("+off+"px)";paint(off);show();
})();"""
    js = js_tpl % {"days": days_json, "by": by_json, "id": div_id}
    # 滚轮容器：3 行可视（44px×3），上下渐变遮罩模拟 iOS 透视效果。
    # items 容器 padding-top/bottom 各 44px：首项/末项也能滚到 mask 框内，
    # transform 以"选中项=容器内 44px（mask 位置）"为基准。
    items_html = "".join(
        f'<div class="wp-item" data-d="{d}">{_fmt_date(d)}</div>' for d in days)
    return (f'<div class="tl-wrap"><div class="tl-head">'
            f'<span class="tl-date">交易日滚轮</span>'
            f'<span class="tl-count" id="tlcount_{div_id}">{title}</span></div>'
            f'<div class="wp" id="wp_{div_id}">'
            f'<div class="wp-fade top"></div>'
            f'<div class="wp-mask"></div>'
            f'<div class="wp-fade bot"></div>'
            f'<div class="wp-items" id="wpi_{div_id}">{items_html}</div>'
            f'</div>'
            f'<div class="tl-trades" id="tlbody_{div_id}"></div></div>'
            f'<script>{js}</script>')

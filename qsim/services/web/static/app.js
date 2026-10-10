(function(){
  function resizeCharts(root){
    if(!root||typeof echarts==="undefined")return;
    root.querySelectorAll(".chart-box,[id^='live_heatmap_']").forEach(function(el){
      var chart=echarts.getInstanceByDom(el);
      if(chart)chart.resize();
    });
    var heatmaps=root.querySelector("#live-heatmap-view");
    if(heatmaps&&heatmaps.style.display!=="none"&&window.__liveHeatmapInit){
      window.__liveHeatmapInit.forEach(function(fn){fn();});
    }
  }
  function activateTab(name,scrollTop){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.toggle('on',x.getAttribute('data-tab')===name);});
    document.querySelectorAll('.tabpage').forEach(function(x){x.classList.toggle('on',x.id==='pg_'+name);});
    var pg=document.getElementById('pg_'+name);
    if(!pg)return;
    try{sessionStorage.setItem('quant_active_tab',name);}catch(e){}
    if(scrollTop)window.scrollTo({top:0,behavior:'smooth'});
    setTimeout(function(){resizeCharts(pg);},70);
  }
  var tabs=document.querySelectorAll('.tab');
  for(var i=0;i<tabs.length;i++){
    (function(t){t.addEventListener('click',function(){
      var name=t.getAttribute('data-tab');
      activateTab(name,true);
    });})(tabs[i]);
  }
  try{
    var saved=sessionStorage.getItem('quant_active_tab');
    if(saved&&document.getElementById('pg_'+saved))activateTab(saved,false);
    var sy=parseInt(sessionStorage.getItem('quant_scroll_y')||'0',10);
    if(sy>0){sessionStorage.removeItem('quant_scroll_y');setTimeout(function(){window.scrollTo(0,sy);},80);}
  }catch(e){}
})();
// Keep only opaque account IDs in session state, never holdings or prices.
(function accountDetails(){
  var key='qsim_open_account_details_v1';
  var selector='details[data-qsim-account-detail="1"]';
  var validId=/^qsim-account-[a-f0-9]{24}$/;
  function sync(detail){
    document.querySelectorAll('[data-qsim-detail-target]').forEach(function(button){
      if(button.getAttribute('data-qsim-detail-target')===detail.id){
        button.setAttribute('aria-expanded',String(detail.open));
      }
    });
  }
  function save(){
    var ids=[];
    document.querySelectorAll(selector).forEach(function(detail){
      if(detail.open&&validId.test(detail.id)&&ids.length<100)ids.push(detail.id);
    });
    try{sessionStorage.setItem(key,JSON.stringify(ids));}catch(_){}
  }
  var saved=[];
  try{
    var raw=sessionStorage.getItem(key);
    if(raw&&raw.length<6000){
      var parsed=JSON.parse(raw);
      if(Array.isArray(parsed))saved=parsed.slice(0,100).filter(function(id){return typeof id==='string'&&validId.test(id);});
    }
  }catch(_){}
  document.querySelectorAll(selector).forEach(function(detail){
    if(saved.indexOf(detail.id)>=0)detail.open=true;
    sync(detail);
  });
  document.addEventListener('click',function(event){
    if(!event.target.closest)return;
    var button=event.target.closest('button[data-qsim-detail-target]');
    if(!button)return;
    var detail=document.getElementById(button.getAttribute('data-qsim-detail-target'));
    if(!detail||!detail.matches(selector))return;
    detail.open=!detail.open;sync(detail);save();
  });
  document.addEventListener('toggle',function(event){
    var detail=event.target;
    if(detail.matches&&detail.matches(selector)){sync(detail);save();}
  },true);
  window.addEventListener('beforeunload',save);
})();
function refreshNow(button){
  var b=button||document.querySelector('.refresh-btn');
  if(!b||b.disabled)return;
  b.disabled=true;
  b.textContent='刷新中…';
  fetch('/refresh',{method:'POST'}).then(function(r){return r.json();})
    .then(function(d){setTimeout(function(){try{sessionStorage.setItem('quant_scroll_y',String(window.scrollY));}catch(e){}location.reload();},800);})
    .catch(function(){b.textContent='失败，重试';setTimeout(function(){b.textContent='刷新';b.disabled=false;},2000);});
}
// 只轮询轻量数据版本号；数据库有新成交、持仓、行情或净值后再重载整页。
(function autoRefreshOnDataChange(){
  var lastVersion=null, busy=false, sseLive=false;
  function onVersion(v){
    if(lastVersion===null){lastVersion=v;return;}
    if(v!==lastVersion){try{sessionStorage.setItem('quant_scroll_y',String(window.scrollY));}catch(e){}location.reload();}
  }
  (function startSse(){
    if(typeof EventSource==='undefined')return;
    try{
      var es=new EventSource('/api/events');
      es.onopen=function(){sseLive=true;};
      es.onmessage=function(e){
        try{var j=JSON.parse(e.data);if(j&&j.type==='data_version')onVersion(j.version);}catch(_){}
      };
      es.onerror=function(){try{es.close();}catch(_){}sseLive=false;};
    }catch(e){}
  })();
  function check(){
    if(sseLive||busy||document.hidden)return;
    busy=true;
    fetch('/api/data-version?_='+Date.now(),{cache:'no-store',credentials:'same-origin'})
      .then(function(r){return r.ok?r.json():null;})
      .then(function(d){
        if(!d||!d.version)return;
        onVersion(d.version);
      })
      .catch(function(){})
      .finally(function(){busy=false;});
  }
  setTimeout(check,3000);
  setInterval(check,15000);
})();

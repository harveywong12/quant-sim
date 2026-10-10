from qsim.services.web import mobile


def test_page_cache_is_invalidated_by_data_changes(monkeypatch):
    version=[1]; calls=[]
    monkeypatch.setattr(mobile,'_RENDER_CACHE',{})
    monkeypatch.setattr(mobile,'_page_version',lambda: version[0])
    monkeypatch.setattr(mobile,'_render_page_uncached',lambda: calls.append(1) or str(len(calls)))
    assert mobile.render_page()=='1'
    assert mobile.render_page()=='1'
    version[0]=2
    assert mobile.render_page()=='2'


def test_page_cache_expires_for_time_based_status(monkeypatch):
    clock=[10.0]; calls=[]
    monkeypatch.setattr(mobile,'_RENDER_CACHE',{})
    monkeypatch.setattr(mobile,'_page_version',lambda: 1)
    monkeypatch.setattr(mobile.time,'monotonic',lambda: clock[0])
    monkeypatch.setattr(mobile,'_render_page_uncached',lambda: calls.append(1) or str(len(calls)))
    assert mobile.render_page()=='1'
    clock[0]+=11
    assert mobile.render_page()=='2'

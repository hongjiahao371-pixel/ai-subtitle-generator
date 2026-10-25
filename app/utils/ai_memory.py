"""Serialise CPU inference and release local model weights between phases."""
import functools
import os
import threading
import time
import urllib.request
import urllib.parse
import json

AI_PHASE_LOCK=threading.RLock()

def exclusive_ai_phase(function):
    @functools.wraps(function)
    def wrapped(*args,**kwargs):
        with AI_PHASE_LOCK:return function(*args,**kwargs)
    return wrapped

def local_memory_mode(cfg):
    return os.environ.get('LOCAL_LLM_MEMORY_MODE','0')=='1' and urllib.parse.urlparse(str(cfg.get('base_url',''))).hostname in {'subtitle-translator','subtitle-editor'} and cfg.get('enabled')

def wait_for_local_sleep(cfg,progress_callback=None,include_editor=True):
    """llama.cpp has an idle sleep timer, not a public forced-sleep endpoint.

    GET /props neither wakes the model nor resets that timer. Never contact an
    external provider with this control request. Fail before a dangerous load.
    """
    if not local_memory_mode(cfg):return
    parsed=urllib.parse.urlparse(cfg['base_url']);url=urllib.parse.urlunparse((parsed.scheme,parsed.netloc,'/props','','',''))
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:
            request=urllib.request.Request(url,headers={'Authorization':'Bearer '+str(cfg.get('api_key',''))})
            with urllib.request.urlopen(request,timeout=8) as response:props=json.load(response)
            if props.get('is_sleeping') is True:
                editor_url=os.environ.get('SUBTITLE_EDITOR_URL','').strip()
                if include_editor and editor_url and parsed.hostname=='subtitle-translator':
                    wait_for_local_sleep(dict(cfg,base_url=editor_url),progress_callback,include_editor=False)
                return
        except Exception:
            pass
        if progress_callback:progress_callback(5,'等待翻译模型释放内存，准备转写...')
        time.sleep(1)
    raise RuntimeError('本地翻译模型未释放内存，已停止加载识别模型以避免内存耗尽')

def release_asr_weights(cache):
    for wrapped in cache.values():
        native=getattr(wrapped,'model',None)
        if native is not None and getattr(native,'model_is_loaded',False):native.unload_model(to_cpu=False)

def asr_weights_loaded(cache):
    return any(getattr(getattr(model,'model',None),'model_is_loaded',True) for model in cache.values())

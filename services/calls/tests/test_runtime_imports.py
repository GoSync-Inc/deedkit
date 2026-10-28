"""Native imports only: no audio device, model credentials or provider session."""
import importlib
import sys
import subprocess
from pathlib import Path


def test_supported_python_and_native_runtime_imports():
    assert sys.version_info[:2] == (3, 13)
    result = subprocess.run([sys.executable, '-c', "import importlib; names=['pipecat.services.google.gemini_live.llm','pipecat.transports.smallwebrtc.transport','pipecat.audio.vad.silero','pipecat.flows','ntgcalls','pytgcalls','av','onnxruntime','voice','live_flows']; [importlib.import_module(name) for name in names]"], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-2000:]
    adapter = importlib.import_module('anymsg')
    assert Path(adapter.__file__).resolve() == Path(__file__).resolve().parents[1] / 'anymsg.py'
    assert hasattr(adapter, 'UnconfiguredArchive')


# spec: ANYM-617
def test_call_records_name_the_installed_pipecat_version(tmp_path):
    from importlib.metadata import version
    from core import BriefInput, VoiceService
    from test_tasks import BRIEF
    assert version('pipecat-ai')=='1.14.0'
    store=VoiceService(tmp_path)
    call=store.start(store.prepare(BriefInput(**BRIEF),[])['id'])
    assert call['engine']=='Pipecat 1.14.0'
    store.close()

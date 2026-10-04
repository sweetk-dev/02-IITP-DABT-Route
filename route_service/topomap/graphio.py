# -*- coding: utf-8 -*-
"""그래프 파일(gpickle) 저장 공용 함수.

빌드·정제 스크립트는 모두 "그래프를 읽어 → 고쳐 → 다른 파일로 저장"한다. 저장을
`open(out, "wb")` 로 바로 하면 두 가지 문제가 생긴다.

  1) 쓰는 도중 중단(디스크 부족·강제 종료)되면 출력 파일이 잘린 채로 남는다. 출력 경로에
     이전 산출물이 있었다면 그것까지 잃는다.
  2) --out 에 --graph 와 같은 경로를 주면 입력이 그 자리에서 덮어써진다. 이 스크립트들은
     같은 입력에 다시 돌리는 것을 전제로 하지 않으므로(횡단보도 반영·접합 보강 등은 누적된다)
     입력을 잃으면 되돌릴 방법이 없다.

그래서 저장은 같은 디렉터리의 임시 파일에 쓴 뒤 os.replace 로 한 번에 바꾸고, 출력 경로가
입력 경로와 같으면 거부한다. 덮어쓰기를 명시적으로 허용한 경우에만 원본을 `<경로>.bak` 으로
복사해 두고 진행한다.

저장 형식은 종전과 같다 — `pickle.dump(G, f)` (기본 프로토콜) 그대로다.
"""
from __future__ import annotations

import os
import pickle
import shutil
import tempfile

BACKUP_SUFFIX = ".bak"


class GraphIOError(ValueError):
    """출력 경로가 입력 경로와 같은데 덮어쓰기가 허용되지 않았을 때."""


def same_path(a, b) -> bool:
    """두 경로가 같은 파일을 가리키는가.

    둘 다 존재하면 os.path.samefile(심볼릭 링크·하드 링크 포함)로, 아니면 정규화한 절대 경로
    문자열로 비교한다. 어느 한쪽이 None/빈 값이면 False.
    """
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def check_output_path(out_path, input_path=None, overwrite_input: bool = False) -> None:
    """출력 경로가 입력 경로와 같으면 GraphIOError 를 낸다(overwrite_input=True 면 통과).

    오래 걸리는 계산을 시작하기 **전에** 스크립트가 불러 일찍 멈추게 하는 용도다.
    save_graph 도 저장 직전에 같은 검사를 한 번 더 한다.

    인자
      out_path        : 저장할 경로. None/빈 값이면 검사하지 않는다(보고서만 내는 실행).
      input_path      : 읽어 들인 그래프 경로. None 이면 검사하지 않는다.
      overwrite_input : 입력 덮어쓰기를 명시적으로 허용했는가.
    """
    if not out_path or not input_path or overwrite_input:
        return
    if same_path(out_path, input_path):
        raise GraphIOError(
            "출력 경로가 입력 그래프와 같습니다: %s — 입력을 덮어쓰면 되돌릴 수 없습니다. "
            "다른 경로를 주거나, 덮어쓰려면 --overwrite-input 을 지정하십시오(원본은 %s 로 남깁니다)."
            % (out_path, str(out_path) + BACKUP_SUFFIX))


def save_graph(G, out_path, input_path=None, overwrite_input: bool = False) -> str:
    """그래프를 out_path 에 원자적으로 저장한다.

    절차
      1) out_path 가 input_path 와 같으면: overwrite_input=False → GraphIOError.
         True → 기존 파일을 `out_path + ".bak"` 으로 복사해 둔다(이미 있는 .bak 은 덮어쓴다).
      2) out_path 와 **같은 디렉터리**에 임시 파일을 만들어 pickle.dump 하고 디스크에 내린다.
         같은 디렉터리여야 os.replace 가 파일 시스템을 넘지 않아 원자적으로 바뀐다.
      3) os.replace 로 교체한다. 그 전에 실패하면 임시 파일만 지우고 예외를 그대로 올린다 —
         out_path 의 기존 내용은 바뀌지 않는다.

    인자
      G               : 저장할 그래프(pickle 가능한 객체).
      out_path        : 저장 경로. 상위 디렉터리는 이미 있어야 한다(종전과 같다).
      input_path      : 이 그래프를 읽어 온 경로. 같은 경로 덮어쓰기 검사에 쓴다.
      overwrite_input : 입력 덮어쓰기 허용 여부.
    반환
      저장한 경로(out_path).
    """
    out_path = os.fspath(out_path)
    check_output_path(out_path, input_path, overwrite_input)
    if input_path and same_path(out_path, input_path) and os.path.exists(out_path):
        shutil.copy2(out_path, out_path + BACKUP_SUFFIX)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(out_path) + ".", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(G, f)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 는 0600 으로 만든다. 종전 open(out, "wb") 와 같은 권한(umask 적용)이 되게 맞춘다 —
        # 산출 파일을 다른 계정의 서비스 프로세스가 읽는 배치에서 권한이 좁아지면 로드가 실패한다.
        umask = os.umask(0)
        os.umask(umask)
        try:
            os.chmod(tmp, 0o666 & ~umask)
        except OSError:
            pass
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out_path

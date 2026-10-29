# SOLARROOF_JIN — Render

압축을 푼 다음 이 폴더 안의 파일과 static 폴더를 GitHub 저장소 최상위에 업로드하세요. ZIP 자체를 올리면 실행되지 않습니다.

Render: New → Web Service → solarroof-jin 저장소 선택.
- Language: Python 3
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn app:application --config gunicorn.conf.py
- Health Check Path: /api/health
- 무료 인스턴스는 사용하지 않으면 절전되며, 재접속 대기가 발생할 수 있습니다.

건축물대장 인증키는 선택 사항입니다. Render Environment에 BUILDING_API_KEY를 등록하세요. 키를 GitHub에 올리지 마세요. 웹 화면을 통한 키 변경은 차단되어 있습니다. 인증키 없이 지도·배치·한전 조회를 사용할 수 있습니다. 정부24 원문 열람 버튼도 제공됩니다.

배포 성공 후 Settings → Custom Domains → solarroof.jinenergy.co.kr 추가.
호스팅케이알 DNS: CNAME / 호스트 solarroof / 값은 Render가 실제 발급한 onrender.com 호스트. 기존 solar 레코드는 유지하세요.
Render로 돌아와 Verify를 누르고 인증서 발급 후 HTTPS 접속을 확인하세요.

포함: 지붕 윤곽/제외영역, 경계1m, 모듈10mm 기본간격, 평지붕 그림자 행간격, 경제성, CSV/DXF, 한전ON 조회, 건축HUB API 연결.
제한: 경사10도는 가정값이며 사진 자동 분석이 아닙니다. 발전량은 입력한 등가발전시간을 사용합니다. DXF는 미터 단위 수평 투영이며 위성사진이 포함되지 않습니다.
한전 결과는 참고자료입니다. 대체 지번 조회 여부와 출처를 확인하세요.
프로젝트는 사용자 브라우저에 저장됩니다. 서버에 프로젝트를 수집하지 않습니다.

Run
cd scripts/maas
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python maas_service.py   # serves on 0.0.0.0:8000

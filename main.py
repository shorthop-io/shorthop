from app import create_app
from app.serve import run

app = create_app()

if __name__ == "__main__":
    run(app)

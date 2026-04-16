from setuptools import setup, find_packages

setup(
    name="olympusrepo",
    version="0.2.0",
    description="OlympusRepo — Sovereign Version Control",
    author="Sean Collins",
    author_email="sean@2pawsmachine.com",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "psycopg2-binary>=2.9",
        "fastapi>=0.100",
        "uvicorn>=0.20",
        "jinja2>=3.1",
        "python-multipart>=0.0.5",
    ],
    entry_points={
        "console_scripts": [
            "olympusrepo=olympusrepo.cli:main",
        ],
    },
)

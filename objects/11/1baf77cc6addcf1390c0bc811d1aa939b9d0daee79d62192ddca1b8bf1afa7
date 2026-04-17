from setuptools import setup, find_packages

setup(
    name="olympusrepo",
    version="0.4.0",
    packages=find_packages(),
    install_requires=[
        "fastapi",
        "uvicorn[standard]",
        "psycopg2-binary",
        "jinja2",
        "python-multipart",
        "cryptography",
        "httpx",
    ],
    entry_points={
        "console_scripts": [
            "olympusrepo=olympusrepo.cli:main",
        ],
    },
    python_requires=">=3.10",
)

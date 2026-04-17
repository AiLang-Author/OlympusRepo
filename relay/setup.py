from setuptools import setup, find_packages

setup(
    name="olympusrelay",
    version="0.1.0",
    description="OlympusRelay — decentralized instance discovery for OlympusRepo",
    author="Sean Collins",
    author_email="sean@2pawsmachine.com",
    license="MIT",
    packages=find_packages(),
    install_requires=[
        "fastapi>=0.100",
        "uvicorn[standard]>=0.20",
        "httpx>=0.24",
        "cryptography>=41.0",
    ],
    entry_points={
        "console_scripts": [
            "olympusrelay=olympusrelay.app:main",
        ],
    },
    python_requires=">=3.10",
)

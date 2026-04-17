from setuptools import setup, find_packages
import os

# Read README for PyPI long description
here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="olympusrepo",
    version="0.5.0",
    description="Sovereign version control. No corporate hooks.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Sean Collins",
    author_email="sean@2pawsmachine.com",
    url="https://github.com/AiLang-Author/OlympusRepo",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "fastapi>=0.100",
        "uvicorn[standard]>=0.20",
        "psycopg2-binary>=2.9",
        "jinja2>=3.1",
        "python-multipart>=0.0.5",
        "cryptography>=41.0",
        "httpx>=0.24",
    ],
    entry_points={
        "console_scripts": [
            "olympusrepo=olympusrepo.cli:main",
            "olympusrepo-setup=olympusrepo.setup_wizard:main",
        ],
    },
    # Include non-Python files
    package_data={
        "olympusrepo": [
            "web/templates/**/*",
            "web/static/**/*",
        ],
    },
    include_package_data=True,
    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Version Control",
        "Environment :: Web Environment",
    ],
    keywords="version control git self-hosted sovereign",
)

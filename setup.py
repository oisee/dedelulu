from setuptools import setup
import subprocess


def _git_version():
    try:
        count = subprocess.check_output(
            ['git', 'rev-list', '--count', 'HEAD'],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        short = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f'0.2.{count}+g{short}'
    except Exception:
        return '0.2.0'


setup(
    name='dedelulu',
    version=_git_version(),
    description='Autonomous supervisor for interactive CLI agents — dedelulu is the solulu',
    python_requires='>=3.10',
    extras_require={
        'anthropic': ['anthropic'],
        'openai': ['openai'],
    },
    py_modules=['dedelulu'],
    entry_points={
        'console_scripts': [
            'dedelulu=dedelulu:main',
            'ddll=dedelulu:main',
            'dedelulu-send=dedelulu:send_main',
            'dedelulu-explore=dedelulu:explore_main',
            'dedelulu-ask=dedelulu:ask_main',
        ],
    },
)

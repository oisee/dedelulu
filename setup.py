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
    name='termiclaude',
    version=_git_version(),
    description='Autonomous supervisor for interactive CLI agents',
    python_requires='>=3.10',
    extras_require={
        'anthropic': ['anthropic'],
        'openai': ['openai'],
    },
    py_modules=['termiclaude', 'termiclaude_multi'],
    entry_points={
        'console_scripts': [
            'termiclaude=termiclaude:main',
            'tc=termiclaude:main',
            'termiclaude-multi=termiclaude_multi:main',
            'tc-multi=termiclaude_multi:main',
        ],
    },
)

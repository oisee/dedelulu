from setuptools import setup

setup(
    name='termiclaude',
    version='0.1.0',
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

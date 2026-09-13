.PHONY: test install dist

test:
	python3 -m unittest -v test_tmux_resume

install:
	sh install.sh

dist:
	mkdir -p dist
	tar -czf dist/tmux-resume.tar.gz tmux_resume.py tmux_resume.vim tmux-resume.el prefill.bash install.sh README.md Makefile test_tmux_resume.py

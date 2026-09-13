# A one-shot Readline prefill for a restored pane. Read the normal user config
# first, and preserve its prompt hooks. No recorded command is evaluated here.
if [[ -r ~/.bashrc ]]; then
    source ~/.bashrc
fi

__tmux_resume_line=${TMUX_RESUME_PREFILL-}
__tmux_resume_key=${TMUX_RESUME_PREFILL_KEY-}
unset TMUX_RESUME_PREFILL TMUX_RESUME_PREFILL_KEY

__tmux_resume_fill() {
    READLINE_LINE=$__tmux_resume_line
    # Readline indexes the buffer in bytes, including for Unicode arguments.
    local LC_ALL=C
    READLINE_POINT=${#READLINE_LINE}
    local keymap
    for keymap in emacs-standard vi-insert vi-command; do
        bind -m "$keymap" -r "\e${__tmux_resume_key}"
    done
    unset __tmux_resume_line __tmux_resume_key
    unset -f __tmux_resume_fill
}

__tmux_resume_first_prompt() {
    # This runs after the user's prompt hooks. Queue only a private Readline
    # binding, never the command text or an Enter key.
    local index keymap
    for index in "${!PROMPT_COMMAND[@]}"; do
        if [[ ${PROMPT_COMMAND[index]} == __tmux_resume_first_prompt ]]; then
            unset 'PROMPT_COMMAND[index]'
        fi
    done
    for keymap in emacs-standard vi-insert vi-command; do
        bind -m "$keymap" -x '"\e'"${__tmux_resume_key}"'":__tmux_resume_fill'
    done
    if ! command tmux send-keys -t "$TMUX_PANE" -l -- $'\e'"$__tmux_resume_key"; then
        printf '[tmux-resume] Could not prefill command: %s\n' "$__tmux_resume_line"
    fi
    unset -f __tmux_resume_first_prompt
}

PROMPT_COMMAND=("${PROMPT_COMMAND[@]}" __tmux_resume_first_prompt)

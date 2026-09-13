" Checkpoint metadata and sessions on request; never write user buffers.
if exists('g:loaded_tmux_resume') || !has('timers') || !exists('*json_encode')
  finish
endif
let g:loaded_tmux_resume = 1
let s:root = (empty($XDG_STATE_HOME) ? expand('~/.local/state') : $XDG_STATE_HOME) . '/tmux-resume/vim'
call mkdir(s:root, 'p', 0700)
let s:base = s:root . '/' . getpid()
let s:stat = split(substitute(join(readfile('/proc/self/stat'), ''), '^.*) ', '', ''))
let s:start = s:stat[19]

function! s:Publish(data) abort
  let l:tmp = s:base . '.json.tmp'
  call writefile([json_encode(a:data)], l:tmp)
  call setfperm(l:tmp, 'rw-------')
  call rename(l:tmp, s:base . '.json')
endfunction

function! s:Checkpoint(request) abort
  let l:data = {'pid': getpid(), 'start': s:start, 'nonce': a:request.nonce, 'buffers': [], 'error': ''}
  for l:b in getbufinfo()
    if l:b.listed || l:b.loaded
      call add(l:data.buffers, {'name': l:b.name, 'modified': l:b.changed, 'line': l:b.lnum, 'buftype': getbufvar(l:b.bufnr, '&buftype')})
      if getbufvar(l:b.bufnr, '&buftype') ==# 'terminal'
        let l:data.error = 'terminal buffers cannot be restored as running jobs'
      endif
    endif
  endfor
  let l:options = &sessionoptions
  let l:session = v:this_session
  try
    let &sessionoptions = 'blank,buffers,curdir,folds,help,tabpages,winsize'
    execute 'silent mksession! ' . fnameescape(a:request.directory . '/session.vim')
    call setfperm(a:request.directory . '/session.vim', 'rw-------')
  catch
    let l:data.error = v:exception
  finally
    let &sessionoptions = l:options
    let v:this_session = l:session
  endtry
  call s:Publish(l:data)
endfunction

function! s:Tick(timer) abort
  let l:path = s:base . '.request.json'
  if filereadable(l:path)
    try
      let l:request = json_decode(join(readfile(l:path), "\n"))
      call delete(l:path)
      call s:Checkpoint(l:request)
    catch
      call s:Publish({'pid': getpid(), 'start': s:start, 'nonce': get(get(l:, 'request', {}), 'nonce', ''), 'buffers': [], 'error': v:exception})
    endtry
  endif
endfunction

call s:Publish({'pid': getpid(), 'start': s:start})
let s:timer = timer_start(200, function('s:Tick'), {'repeat': -1})
augroup tmux_resume
  autocmd!
  autocmd VimLeavePre * call delete(s:base . '.json') | call delete(s:base . '.request.json')
augroup END

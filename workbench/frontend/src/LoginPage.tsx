import React, { useEffect, useState } from 'react'
import { api } from './api'

/**
 * 登录页：身份由服务端会话决定。
 * 页面不再让用户"选择一个角色进入"——角色来自服务端 /api/auth/me。
 *
 * 本地开发便利：默认账号/密码可由本机 .env.local 在构建时注入（VITE_LOGIN_DEFAULT_*），
 * 打开页面即已填好，直接点「登录」即可。该文件已被 gitignore，仓库内不保存任何口令。
 */
const DEFAULT_USER = import.meta.env.VITE_LOGIN_DEFAULT_USER || 'usr_default'
const DEFAULT_PASSWORD = import.meta.env.VITE_LOGIN_DEFAULT_PASSWORD || ''
export default function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [userId, setUserId] = useState(DEFAULT_USER)
  const [password, setPassword] = useState('')
  const [needsBootstrap, setNeedsBootstrap] = useState<boolean | null>(null)
  const [name, setName] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    // 仅在「需要登录」而非首次初始化时预填默认密码，避免影响 bootstrap 设置新密码
    if (needsBootstrap === false && DEFAULT_PASSWORD) setPassword((cur) => cur || DEFAULT_PASSWORD)
  }, [needsBootstrap])

  useEffect(() => {
    api.authBootstrapState().then((s) => {
      setNeedsBootstrap(!!s.needs_bootstrap)
      if (s.dev_mode) setErr('⚠ 当前服务端开启了 WB_DEV_AUTH 开发放行模式，请勿在生产环境使用。')
    }).catch(() => setNeedsBootstrap(false))
  }, [])

  const doLogin = async () => {
    setErr(''); setBusy(true)
    try {
      await api.authLogin({ user_id: userId.trim(), password })
      onLogin()
    } catch (e: any) {
      setErr(String(e?.message || e).includes('401') ? '用户名或密码错误，或账号已被禁用' : String(e?.message || e))
    } finally { setBusy(false) }
  }

  const doBootstrap = async () => {
    setErr('')
    if (password.length < 8) { setErr('密码至少 8 位'); return }
    if (password !== confirm) { setErr('两次输入的密码不一致'); return }
    setBusy(true)
    try {
      await api.authBootstrap({ user_id: userId.trim(), password, name: name.trim() || undefined })
      onLogin()
    } catch (e: any) {
      setErr(String(e?.message || e))
    } finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <h1>AI 生图工作台</h1>
        {needsBootstrap ? (
          <>
            <p className="muted">首次使用：初始化管理员账号与密码（仅此次有效）</p>
            <div className="field"><label>管理员账号</label>
              <input value={userId} onChange={(e) => setUserId(e.target.value)} /></div>
            <div className="field"><label>显示名</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：张三" /></div>
            <div className="field"><label>设置密码（至少 8 位）</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
            <div className="field"><label>确认密码</label>
              <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') doBootstrap() }} /></div>
            <button className="btn primary" style={{ width: '100%' }} disabled={busy} onClick={doBootstrap}>
              {busy ? '初始化中…' : '初始化并进入'}
            </button>
          </>
        ) : (
          <>
            <p className="muted">请使用账号密码登录</p>
            <div className="field"><label>账号</label>
              <input value={userId} onChange={(e) => setUserId(e.target.value)} autoComplete="username" /></div>
            <div className="field"><label>密码</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password" onKeyDown={(e) => { if (e.key === 'Enter') doLogin() }} /></div>
            {DEFAULT_PASSWORD ? <div className="hint">已按本机 .env.local 预填默认账号，可直接点「登录」。</div> : null}
            <button className="btn primary" style={{ width: '100%' }} disabled={busy} onClick={doLogin}>
              {busy ? '登录中…' : '登录'}
            </button>
          </>
        )}
        {err && <div className="login-err" style={{ color: '#B42318', fontSize: 12, marginTop: 10 }}>{err}</div>}
      </div>
    </div>
  )
}

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 本地开发默认登录账号：由 .env.local 注入，不入库 */
  readonly VITE_LOGIN_DEFAULT_USER?: string
  /** 本地开发默认登录密码：由 .env.local 注入，不入库 */
  readonly VITE_LOGIN_DEFAULT_PASSWORD?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

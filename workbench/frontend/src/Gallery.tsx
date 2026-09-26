import React, { useState } from 'react'
import { Template, AspectRatio } from './types'

const LEVELS = [
  { key: 'brand', name: '品牌视觉', subs: ['品牌｜视觉系统', '品牌｜Logo与VI', '品牌｜品牌模板', '品牌｜包装与空间'] },
  { key: 'marketing', name: '营销设计', subs: ['营销｜品牌海报', '营销｜产品海报', '营销｜服务海报', '营销｜活动海报', '营销｜促销海报', '营销｜背书/案例海报', '营销｜系列节点海报'] },
  { key: 'content', name: '内容视觉', subs: ['内容｜视频封面', '内容｜图文封面', '内容｜直播/课程封面', '内容｜信息图', '内容｜传播长图', '内容｜社媒轮播图'] },
  { key: 'ecommerce', name: '电商视觉', subs: ['电商｜产品广告图', '电商｜电商主图', '电商｜详情页', '电商｜场景图', '电商｜细节/参数图'] },
  { key: 'illustration', name: '插图素材', subs: ['素材｜内容插图', '素材｜场景概念图', '素材｜角色IP', '素材｜图标纹样', '素材｜背景与合成素材'] },
  { key: 'tool', name: '工具系统', subs: ['工具｜视觉策略', '工具｜参考图拆解', '工具｜生图提示词', '工具｜文案工具', '系统｜工作流', '系统｜方法论', '系统｜审核规范'] },
]

const TPL = (id: string, name: string, subtitle: string, aspect: AspectRatio, accent: string, level1: string, level2: string, skeleton: string): Template =>
  ({ id, name, subtitle, aspect, accent, level1, level2, skeleton })

const TEMPLATES: Template[] = [
  TPL('b1', 'Logo / VI 设计', '品牌标识与视觉规范', '1:1', '#7C3AED', 'brand', '品牌｜Logo与VI', 'illustration'),
  TPL('b2', '品牌视觉系统', '色彩 / 字体 / 版式规范', '4:3', '#6366F1', 'brand', '品牌｜视觉系统', 'illustration'),
  TPL('m1', '品牌形象海报', '品牌主张 / 价值表达', '3:4', '#7C3AED', 'marketing', '营销｜品牌海报', 'poster'),
  TPL('m2', '产品卖点海报', '主卖点 + 场景 + 证明', '1:1', '#3B82F6', 'marketing', '营销｜产品海报', 'poster'),
  TPL('m3', '服务承诺海报', '风险反转 / 无醛承诺', '3:4', '#10B981', 'marketing', '营销｜服务海报', 'poster'),
  TPL('m4', '活动传播海报', '活动主题 + 节点 + 权益', '3:4', '#EF4444', 'marketing', '营销｜活动海报', 'poster'),
  TPL('m5', '促销转化海报', '算账 + 权益 + 限时', '3:4', '#F59E0B', 'marketing', '营销｜促销海报', 'poster'),
  TPL('m6', '信任背书海报', '检测 / 人物 / 案例', '3:4', '#6366F1', 'marketing', '营销｜背书/案例海报', 'poster'),
  TPL('m7', '系列节点海报', '发布会 / 倒计时 / 悬念', '3:4', '#8B5CF6', 'marketing', '营销｜系列节点海报', 'poster'),
  TPL('c1', '视频号 / 抖音封面', '竖版封面 · 9:16', '9:16', '#F59E0B', 'content', '内容｜视频封面', 'cover'),
  TPL('c2', '公众号封面', '大字标题 · 16:9', '16:9', '#10B981', 'content', '内容｜图文封面', 'cover'),
  TPL('c3', '小红书图文封面', '种草 / 攻略 · 3:4', '3:4', '#EC4899', 'content', '内容｜图文封面', 'cover'),
  TPL('c4', '朋友圈海报', '悬念 / 金句 · 3:4', '3:4', '#3B82F6', 'content', '内容｜图文封面', 'cover'),
  TPL('c5', '会后成果长图', '结论 + 过程 + 成果', 'a4', '#3B82F6', 'content', '内容｜传播长图', 'long'),
  TPL('c6', '攻略 / 科普信息图', '误区 + 判断工具', 'a4', '#14B8A6', 'content', '内容｜信息图', 'long'),
  TPL('c7', '社媒轮播图', '多张轮播 · 3:4', '3:4', '#EC4899', 'content', '内容｜社媒轮播图', 'detail'),
  TPL('e1', '电商主图套图', '全套主图 · 1:1', '1:1', '#7C3AED', 'ecommerce', '电商｜电商主图', 'detail'),
  TPL('e2', '商品详情页', '参数 / 卖点 / 场景', 'a4', '#6366F1', 'ecommerce', '电商｜详情页', 'detail'),
  TPL('e3', '商品场景图', '场景适配 · 多尺寸', '3:4', '#EC4899', 'ecommerce', '电商｜场景图', 'poster'),
  TPL('e4', '细节 / 参数图', '特写 + 参数标注', '1:1', '#14B8A6', 'ecommerce', '电商｜细节/参数图', 'poster'),
  TPL('i1', '场景插图', '生活场景 / 使用示意', '1:1', '#8B5CF6', 'illustration', '素材｜内容插图', 'illustration'),
  TPL('i2', '编辑插图', '图文配图 / 栏目插图', '4:3', '#14B8A6', 'illustration', '素材｜内容插图', 'illustration'),
  TPL('i3', '角色 / IP 与吉祥物', '品牌 IP 形象', '1:1', '#F59E0B', 'illustration', '素材｜角色IP', 'illustration'),
]

const ASPECT_LABEL: Record<AspectRatio, string> = { free: '自由', '16:9': '16:9', '4:3': '4:3', '1:1': '1:1', '3:4': '3:4', '9:16': '9:16', a4: 'A4' }
const RATIO: Record<AspectRatio, string> = { free: '4 / 3', '16:9': '16 / 9', '4:3': '4 / 3', '1:1': '1 / 1', '3:4': '3 / 4', '9:16': '9 / 16', a4: '210 / 297' }

interface Props {
  onUse: (t: Template) => void
  onWorkflow: () => void
  onDesign: () => void
  onAdmin?: () => void
}

export default function Gallery({ onUse, onWorkflow, onDesign, onAdmin }: Props) {
  const [level, setLevel] = useState('marketing')
  const [sub, setSub] = useState('')

  const cur = LEVELS.find((l) => l.key === level)!
  const shown = TEMPLATES.filter((t) => t.level1 === level && (!sub || t.level2 === sub))

  return (
    <div className="gallery">
      <header className="gallery-head">
        <div>
          <h1>企业获客物料创作台</h1>
          <p>面向行业企业的终端获客物料。选择模版开始，或进入节点式生图工作流。</p>
        </div>
        <button className="btn primary" onClick={onWorkflow}>进入生图工作流</button>
        <button className="btn" onClick={onDesign}>自由设计画布</button>
        {onAdmin && <button className="btn" onClick={onAdmin}>后台管理</button>}
      </header>

      <div className="level-tabs">
        {LEVELS.map((l) => (
          <button key={l.key} className={`level-tab ${level === l.key ? 'on' : ''}`} onClick={() => { setLevel(l.key); setSub('') }}>{l.name}</button>
        ))}
      </div>

      <div className="tag-bar">
        <button className={`tag-chip ${sub === '' ? 'on' : ''}`} onClick={() => setSub('')}>全部</button>
        {cur.subs.map((s) => (
          <button key={s} className={`tag-chip ${sub === s ? 'on' : ''}`} onClick={() => setSub(s)}>{s}</button>
        ))}
      </div>

      {shown.length > 0 ? (
        <div className="gallery-grid">
          {shown.map((t) => (
            <button className="tpl-card" key={t.id} onClick={() => onUse(t)}>
              <div className="tpl-thumb" style={{ aspectRatio: RATIO[t.aspect], background: t.accent }}><span>{ASPECT_LABEL[t.aspect]}</span></div>
              <div className="tpl-body"><div className="tpl-name">{t.name}</div><div className="tpl-sub">{t.subtitle}</div></div>
            </button>
          ))}
        </div>
      ) : (
        <div className="sub-list">
          {cur.subs.map((s) => (
            <div className="sub-card" key={s}><div className="sub-card-name">{s}</div><div className="sub-card-hint">该二级类型暂无物料模板</div></div>
          ))}
        </div>
      )}
    </div>
  )
}

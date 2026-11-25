import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/es/locale/zh_CN'
import App from './App'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: {
      colorPrimary: '#007b75',
      colorText: '#172b3b',
      colorTextSecondary: '#526879',
      colorTextDescription: '#526879',
      colorBorder: '#d5e0e7',
      colorBorderSecondary: '#e3ebf1',
      borderRadius: 8,
      fontFamily: 'system-ui, "PingFang SC", "Microsoft YaHei", sans-serif',
    }, components: { Table: {
      rowSelectedBg: '#f0f9f7',
      rowSelectedHoverBg: '#eaf6f3',
    } } }}>
      <App />
    </ConfigProvider>
  </StrictMode>,
)

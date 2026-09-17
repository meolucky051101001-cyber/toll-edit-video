import React, { useState, useRef, useEffect } from 'react'
import './App.css'

// Electron exposes only the two file-picker operations through a sandboxed
// preload bridge. Browser preview mode intentionally has no desktop access.
const desktopBridge = window.autodub || null;

function App() {
  const [selectedFolder, setSelectedFolder] = useState(null);
  const [videos, setVideos] = useState([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [selectedVideo, setSelectedVideo] = useState(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState('00:00:00');
  const [duration, setDuration] = useState('00:00:00');
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [activeRightTab, setActiveRightTab] = useState('video');
  const [activeRightSubTab, setActiveRightSubTab] = useState('basic');
  const [activeLeftTab, setActiveLeftTab] = useState('media');
  const [subtitles, setSubtitles] = useState([]);
  const [processLog, setProcessLog] = useState('');
  const [voiceSource, setVoiceSource] = useState('edge');
  const [voiceConfig, setVoiceConfig] = useState('');
  
  // Log viewer state
  const [showLogs, setShowLogs] = useState(false);
  const [botLogs, setBotLogs] = useState('');

  const videoRef = useRef(null);

  useEffect(() => {
    let interval;
    if (showLogs) {
      interval = setInterval(async () => {
        try {
          const res = await fetch('http://localhost:8000/api/logs');
          if (res.ok) {
            const data = await res.json();
            setBotLogs(data.logs);
          }
        } catch (e) {
          console.error("Failed to fetch logs:", e);
        }
      }, 2000);
    }
    return () => clearInterval(interval);
  }, [showLogs]);

  const formatTime = (secs) => {
    const h = Math.floor(secs / 3600).toString().padStart(2, '0');
    const m = Math.floor((secs % 3600) / 60).toString().padStart(2, '0');
    const s = Math.floor(secs % 60).toString().padStart(2, '0');
    return `${h}:${m}:${s}`;
  };

  const handleSelectFolder = async () => {
    if (desktopBridge) {
      const result = await desktopBridge.openDirectory();
      if (result && result.folderPath) {
        setSelectedFolder(result.folderPath);
        setVideos(result.videoFiles);
        if (result.videoFiles.length > 0) {
          setSelectedVideo(result.videoFiles[0].path);
        }
      }
    } else {
      alert("Tính năng này chỉ hoạt động trên Electron Desktop App.");
    }
  };

  const handleSelectFiles = async () => {
    if (desktopBridge) {
      const result = await desktopBridge.openFiles();
      if (result && result.videoFiles.length > 0) {
        setSelectedFolder(result.folderPath);
        setVideos(prev => [...prev, ...result.videoFiles]);
        if (!selectedVideo) {
          setSelectedVideo(result.videoFiles[0].path);
        }
      }
    } else {
      alert("Tính năng này chỉ hoạt động trên Electron Desktop App.");
    }
  };

  const handlePlayPause = () => {
    if (videoRef.current) {
      if (videoRef.current.paused) {
        videoRef.current.play();
        setIsPlaying(true);
      } else {
        videoRef.current.pause();
        setIsPlaying(false);
      }
    }
  };

  const handleTimeUpdate = () => {
    if (videoRef.current) {
      setCurrentTime(formatTime(videoRef.current.currentTime));
    }
  };

  const handleLoadedMetadata = () => {
    if (videoRef.current) {
      setDuration(formatTime(videoRef.current.duration));
    }
  };

  const handleChangeSpeed = (speed) => {
    if (videoRef.current) {
      videoRef.current.playbackRate = speed;
      setPlaybackSpeed(speed);
    }
  };

  const handleSkip = (seconds) => {
    if (videoRef.current) {
      videoRef.current.currentTime += seconds;
    }
  };

  const handleProcess = async () => {
    if (videos.length === 0) return;
    setIsProcessing(true);
    setProcessLog('Đang xử lý lồng tiếng...');
    try {
      // Get the real file path (strip local:/// prefix)
      const filePath = selectedVideo.replace('local:///', '').replace(/\//g, '\\');
      const formData = new FormData();
      formData.append('video_path', filePath);
      formData.append('target_lang', 'vi');
      formData.append('voice_source', voiceSource);
      formData.append(
        'voice_param',
        voiceSource === 'rvc'
          ? voiceConfig.trim()
          : 'vi-VN-HoaiMyNeural'
      );
      formData.append('api_key', voiceSource === 'fpt' ? voiceConfig.trim() : '');

      const res = await fetch('http://127.0.0.1:8000/api/process_video', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (data.status === 'success') {
        setProcessLog(`✅ Hoàn tất! Video: ${data.final_video}`);
        alert(`Xuất video thành công!\n${data.final_video}`);
      } else {
        setProcessLog(`❌ Lỗi: ${data.message}`);
      }
    } catch (err) {
      setProcessLog(`❌ Không kết nối được Backend: ${err.message}`);
    }
    setIsProcessing(false);
  };

  const handleGenerateSubtitles = async () => {
    if (!selectedVideo) return;
    setIsProcessing(true);
    setProcessLog('🔄 Đang tạo phụ đề (Whisper AI + Google Translate)...');
    try {
      const filePath = selectedVideo.replace('local:///', '').replace(/\//g, '\\');
      const formData = new FormData();
      formData.append('video_path', filePath);
      formData.append('target_lang', 'vi');

      const res = await fetch('http://127.0.0.1:8000/api/generate_subtitles', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (data.status === 'success') {
        setSubtitles(data.subtitles);
        setProcessLog(`✅ Tạo phụ đề thành công! Tổng: ${data.total} đoạn`);
      } else {
        setProcessLog(`❌ Lỗi: ${data.message}`);
      }
    } catch (err) {
      setProcessLog(`❌ Không kết nối được Backend: ${err.message}`);
    }
    setIsProcessing(false);
  };

  const [urlInput, setUrlInput] = useState('');

  const handleProcessUrl = async () => {
    if (!urlInput.trim()) return;
    setIsProcessing(true);
    setProcessLog('📥 Đang tải video từ link...');
    try {
      const formData = new FormData();
      formData.append('url', urlInput.trim());
      formData.append('target_lang', 'vi');
      formData.append('voice_source', voiceSource);
      formData.append(
        'voice_param',
        voiceSource === 'rvc'
          ? voiceConfig.trim()
          : 'vi-VN-HoaiMyNeural'
      );
      formData.append('api_key', voiceSource === 'fpt' ? voiceConfig.trim() : '');

      const res = await fetch('http://127.0.0.1:8000/api/process_url', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (data.status === 'success') {
        setSubtitles(data.subtitles || []);
        setProcessLog(`✅ ${data.message}`);
        alert(`Hoàn tất!\nVideo: ${data.final_video}`);
        // Load the final video into preview
        if (data.final_video) {
          const previewPath = 'local:///' + data.final_video.replace(/\\/g, '/');
          setSelectedVideo(previewPath);
          setVideos(prev => [...prev, { name: data.final_video.split(/[\\/]/).pop(), path: previewPath }]);
        }
      } else {
        setProcessLog(`❌ Lỗi (${data.step || 'unknown'}): ${data.message}`);
      }
    } catch (err) {
      setProcessLog(`❌ Không kết nối được Backend: ${err.message}`);
    }
    setIsProcessing(false);
    setUrlInput('');
  };

  return (
    <div className="app-container">
      {/* ===== TOP BAR ===== */}
      <div className="topbar">
        <div className="topbar-left">
          <div className="topbar-logo">
            <div className="logo-icon">V</div>
            AutoDub
          </div>
          <div className="topbar-menu">
            <button className="topbar-menu-btn">Menu ▾</button>
          </div>
        </div>
        <div className="topbar-center">
          <div className="save-indicator">
            <div className="save-dot"></div>
            Tự động lưu
          </div>
          <span style={{color: 'var(--text-bright)'}}>{selectedFolder ? selectedFolder.split('\\').pop() : 'Chưa có dự án'}</span>
        </div>
        <div className="topbar-right">
          <button className="topbar-btn">👤 Chia sẻ</button>
          <button
            className="topbar-btn process"
            onClick={handleProcess}
            disabled={isProcessing || videos.length === 0}
          >
            {isProcessing ? '⏳ Đang xử lý...' : '🚀 Bắt Đầu Lồng Tiếng'}
          </button>
          <button className="topbar-btn export">📤 Xuất</button>
        </div>
      </div>

      {/* ===== TOOLBAR TABS ===== */}
      <div className="toolbar-tabs">
        {[
          {icon: '📁', label: 'Tập phương tiện', id: 'media'},
          {icon: '🔊', label: 'Âm thanh', id: 'audio'},
          {icon: '🔤', label: 'Văn bản', id: 'text'},
          {icon: '😀', label: 'Nhãn dán', id: 'sticker'},
          {icon: '✨', label: 'Hiệu ứng', id: 'effects'},
          {icon: '🔀', label: 'Chuyển tiếp', id: 'transition'},
          {icon: '📝', label: 'Chú thích', id: 'annotate'},
          {icon: '🎨', label: 'Bộ lọc', id: 'filter'},
        ].map(tab => (
          <button
            key={tab.id}
            className={`tab-item ${activeLeftTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveLeftTab(tab.id)}
          >
            <span className="tab-icon">{tab.icon}</span>
            <span className="tab-label">{tab.label}</span>
          </button>
        ))}
      </div>

      {/* ===== MAIN WORKSPACE ===== */}
      <div className="main-workspace">
        {/* === LEFT PANEL === */}
        <aside className="left-panel">
          <div className="left-panel-header">
            <button className="import-btn" onClick={handleSelectFiles}>
              📄 Chọn File Video
            </button>
            <button className="import-btn" onClick={handleSelectFolder} style={{borderColor: 'var(--border-light)'}}>
              📁 Chọn Thư mục
            </button>

            {/* URL Input */}
            <div style={{display: 'flex', gap: '4px', marginTop: '4px'}}>
              <input
                className="prop-input-full"
                placeholder="🔗 Dán link video (Xiaohongshu, TikTok...)"
                value={urlInput}
                onChange={e => setUrlInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleProcessUrl(); }}
                style={{flex: 1, fontSize: '11px'}}
              />
              <button
                className="import-btn"
                onClick={handleProcessUrl}
                disabled={!urlInput.trim() || isProcessing}
                style={{
                  width: '60px', minWidth: '60px', padding: '6px',
                  fontSize: '11px', flexShrink: 0,
                  color: isProcessing ? 'var(--text-muted)' : 'var(--accent-orange)'
                }}
              >
                {isProcessing ? '⏳' : '🚀 Tải'}
              </button>
            </div>

            <div className="left-panel-tabs">
              <button className={`left-tab ${activeLeftTab === 'media' ? 'active' : ''}`}
                onClick={() => setActiveLeftTab('media')}>Thư viện</button>
              <button className={`left-tab ${activeLeftTab === 'text' ? 'active' : ''}`}
                onClick={() => setActiveLeftTab('text')}>Văn bản</button>
            </div>
          </div>

          {activeLeftTab === 'text' && (
            <div className="left-panel-tools">
              <button
                className="import-btn"
                onClick={handleGenerateSubtitles}
                disabled={!selectedVideo || isProcessing}
                style={{marginBottom: '8px', color: isProcessing ? 'var(--text-muted)' : 'var(--primary)'}}
              >
                {isProcessing ? '⏳ Đang tạo...' : '🤖 Tạo phụ đề tự động (AI)'}
              </button>

              {processLog && (
                <div style={{
                  padding: '8px', marginBottom: '8px', borderRadius: '4px',
                  background: 'var(--bg-input)', fontSize: '11px', color: 'var(--text-main)',
                  lineHeight: '1.5', wordBreak: 'break-all'
                }}>
                  {processLog}
                </div>
              )}

              {subtitles.length > 0 && (
                <div style={{flex: 1, overflowY: 'auto'}}>
                  <div style={{fontSize: '11px', color: 'var(--text-muted)', marginBottom: '6px'}}>
                    📝 {subtitles.length} đoạn phụ đề:
                  </div>
                  {subtitles.map((sub, i) => (
                    <div key={i} style={{
                      padding: '6px 8px', marginBottom: '3px', borderRadius: '4px',
                      background: 'var(--bg-input)', fontSize: '11px', cursor: 'pointer',
                      borderLeft: '3px solid var(--primary)'
                    }}
                    onClick={() => { if (videoRef.current) videoRef.current.currentTime = sub.start; }}
                    >
                      <div style={{color: 'var(--text-muted)', fontSize: '9px'}}>
                        {formatTime(sub.start)} → {formatTime(sub.end)}
                      </div>
                      <div style={{color: 'var(--text-main)', marginTop: '2px'}}>{sub.content}</div>
                    </div>
                  ))}
                </div>
              )}

              {subtitles.length === 0 && !processLog && (
                <div className="empty-text" style={{marginTop: '20px'}}>
                  Chọn video rồi bấm nút ở trên<br/>để AI tạo phụ đề tự động
                </div>
              )}
            </div>
          )}

          {activeLeftTab !== 'text' && (
            <div className="file-list">
              {videos.length === 0 ? (
                <div className="empty-text">
                  <div style={{fontSize: '30px', marginBottom: '8px', opacity: 0.3}}>📂</div>
                  Chưa có tệp nào<br/>
                  <span style={{fontSize: '11px'}}>Bấm "Nhập tệp" để bắt đầu</span>
                </div>
              ) : (
                videos.map((vid, idx) => (
                  <div
                    key={idx}
                    className={`file-item ${selectedVideo === vid.path ? 'active' : ''}`}
                    onClick={() => setSelectedVideo(vid.path)}
                  >
                    <div className="file-thumb">🎬</div>
                    <span className="file-name">{vid.name}</span>
                  </div>
                ))
              )}
            </div>
          )}
        </aside>

        {/* === CENTER PREVIEW === */}
        <section className="center-preview">
          <div className="preview-canvas">
            {selectedVideo ? (
              <video
                ref={videoRef}
                src={selectedVideo}
                onTimeUpdate={handleTimeUpdate}
                onLoadedMetadata={handleLoadedMetadata}
                onEnded={() => setIsPlaying(false)}
              />
            ) : (
              <div className="preview-placeholder">
                <div className="ph-icon">▶</div>
                Chọn video để xem trước
              </div>
            )}
          </div>

          {/* Player controls */}
          <div className="player-controls">
            <span className="time-display">{currentTime}</span>
            <span className="time-display" style={{color: 'var(--text-muted)'}}>/</span>
            <span className="time-display">{duration}</span>

            <button className="player-btn" onClick={() => handleSkip(-5)} title="Lùi 5s">⏮</button>
            <button className="player-btn play-btn" onClick={handlePlayPause} title="Play/Pause">
              {isPlaying ? '⏸' : '▶'}
            </button>
            <button className="player-btn" onClick={() => handleSkip(5)} title="Tiến 5s">⏭</button>

            {[0.5, 1, 1.5, 2].map(s => (
              <button
                key={s}
                className={`speed-btn ${playbackSpeed === s ? 'active-speed' : ''}`}
                onClick={() => handleChangeSpeed(s)}
              >
                {s}x
              </button>
            ))}

            <div className="player-controls-right">
              <button className="fit-btn">Vừa khung hình</button>
            </div>
          </div>
        </section>

        {/* === RIGHT PANEL (Properties) === */}
        <aside className="right-panel">
          <div className="right-panel-tabs">
            {[
              {id: 'video', label: 'Video'},
              {id: 'audio', label: 'Âm thanh'},
              {id: 'speed', label: 'Tốc độ'},
              {id: 'adjust', label: 'Điều chỉnh'},
            ].map(tab => (
              <button
                key={tab.id}
                className={`right-tab ${activeRightTab === tab.id ? 'active' : ''}`}
                onClick={() => setActiveRightTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Sub-tabs */}
          <div className="right-panel-sub-tabs">
            {[
              {id: 'basic', label: 'Cơ bản'},
              {id: 'blur', label: 'Làm mờ'},
              {id: 'mask', label: 'Mặt nạ'},
              {id: 'smooth', label: 'Làm mịn'},
            ].map(tab => (
              <button
                key={tab.id}
                className={`right-sub-tab ${activeRightSubTab === tab.id ? 'active' : ''}`}
                onClick={() => setActiveRightSubTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="right-panel-content">
            {/* Transform section */}
            {activeRightSubTab === 'basic' && (
              <>
                <div className="prop-section">
                  <div className="prop-section-title">
                    Biến đổi <button className="reset-btn" title="Reset">↺</button>
                  </div>
                  <div className="prop-row">
                    <span className="prop-label">Tỷ lệ</span>
                    <input type="range" className="prop-slider" min="0" max="200" defaultValue="100" />
                    <input className="prop-input" defaultValue="100%" />
                  </div>
                </div>

                <div className="prop-section">
                  <div className="prop-row">
                    <span className="prop-label">Vị trí</span>
                    <span style={{fontSize: '10px', color: 'var(--text-muted)'}}>X</span>
                    <input className="prop-input" defaultValue="0" />
                    <span style={{fontSize: '10px', color: 'var(--text-muted)'}}>Y</span>
                    <input className="prop-input" defaultValue="0" />
                  </div>
                  <div className="prop-row">
                    <span className="prop-label">Xoay</span>
                    <input className="prop-input" defaultValue="0.00°" style={{width: '70px'}} />
                  </div>
                </div>

                <div className="prop-section">
                  <div className="alignment-btns">
                    {['⇤', '⇥', '⇧', '⇩', '⊞', '⊟'].map((icon, i) => (
                      <button key={i} className="align-btn" title="Căn chỉnh">{icon}</button>
                    ))}
                  </div>
                </div>

                {/* Voice settings */}
                <div className="prop-section">
                  <div className="prop-section-title">🎤 Giọng lồng tiếng</div>
                  <div style={{marginBottom: '8px'}}>
                    <span className="prop-label" style={{width: 'auto', marginBottom: '4px', display: 'block'}}>Nguồn giọng</span>
                    <select
                      className="prop-select"
                      value={voiceSource}
                      onChange={event => {
                        setVoiceSource(event.target.value);
                        setVoiceConfig('');
                      }}
                    >
                      <option value="edge">Edge TTS (Miễn phí)</option>
                      <option value="fpt">FPT AI (API Key)</option>
                      <option value="rvc">RVC Model (Offline)</option>
                    </select>
                  </div>
                  <div>
                    <span className="prop-label" style={{width: 'auto', marginBottom: '4px', display: 'block'}}>
                      {voiceSource === 'rvc' ? 'Đường dẫn model .pth' : 'FPT API Key'}
                    </span>
                    <input
                      className="prop-input-full"
                      value={voiceConfig}
                      disabled={voiceSource === 'edge'}
                      onChange={event => setVoiceConfig(event.target.value)}
                      placeholder={
                        voiceSource === 'rvc'
                          ? 'D:\\models\\voice.pth (để trống để tự tìm model)'
                          : voiceSource === 'fpt'
                            ? 'Nhập FPT API Key...'
                            : 'Edge TTS không cần cấu hình'
                      }
                    />
                  </div>
                </div>

                {/* Subtitle styling */}
                <div className="prop-section">
                  <div className="prop-section-title">📝 Định dạng phụ đề</div>
                  <div style={{display: 'flex', gap: '8px', marginBottom: '8px'}}>
                    <div style={{flex: 1}}>
                      <span className="prop-label" style={{width: 'auto', marginBottom: '4px', display: 'block'}}>Phông chữ</span>
                      <select className="prop-select">
                        <option>Arial</option>
                        <option>Roboto</option>
                        <option>Inter</option>
                        <option>Times New Roman</option>
                      </select>
                    </div>
                    <div style={{flex: 1}}>
                      <span className="prop-label" style={{width: 'auto', marginBottom: '4px', display: 'block'}}>Độ đậm</span>
                      <select className="prop-select">
                        <option value="1">Thường</option>
                        <option value="2">Đậm</option>
                      </select>
                    </div>
                  </div>
                  <div>
                    <span className="prop-label" style={{width: 'auto', marginBottom: '4px', display: 'block'}}>Màu chữ</span>
                    <input className="prop-input-full" defaultValue="&H00FFFFFF" />
                  </div>
                </div>
              </>
            )}

            {activeRightSubTab === 'blur' && (
              <div className="prop-section">
                <div className="prop-section-title">Vùng làm mờ (xóa sub cũ)</div>
                <select className="prop-select" style={{marginBottom: '12px'}}>
                  <option>Cạnh dưới (20%)</option>
                  <option>Cạnh dưới (15%)</option>
                  <option>Không làm mờ</option>
                </select>
                <div className="prop-section-title">Render Engine</div>
                <select className="prop-select" disabled>
                  <option>Hardware (CUDA/NVENC) - RTX 4050</option>
                </select>
              </div>
            )}

            {/* Toggle features */}
            <div className="prop-toggle-row">
              <span className="prop-toggle-label">🎯 Ổn định hình ảnh</span>
              <button className="toggle-switch" />
            </div>
            <div className="prop-toggle-row">
              <span className="prop-toggle-label">🔮 Cải thiện chất lượng</span>
              <button className="toggle-switch" />
            </div>
            <div className="prop-toggle-row">
              <span className="prop-toggle-label">📉 Giảm nhiễu hình ảnh</span>
              <button className="toggle-switch" />
            </div>
          </div>
        </aside>
      </div>

      {/* ===== TIMELINE ===== */}
      <footer className="timeline-section">
        {/* Timeline toolbar */}
        <div className="timeline-toolbar">
          <button className="tl-tool-btn" title="Thêm">＋</button>
          <button className="tl-tool-btn" title="Hoàn tác">↩</button>
          <button className="tl-tool-btn" title="Làm lại">↪</button>
          <div className="tl-separator" />
          <button className="tl-tool-btn" title="Cắt">✂</button>
          <button className="tl-tool-btn" title="Xóa">🗑</button>
          <button className="tl-tool-btn" title="Sao chép">📋</button>
          <button className="tl-tool-btn" title="Ghim">📌</button>
          <div className="tl-separator" />
          <button className="tl-tool-btn" title="Tách âm">🔈</button>
          <button className="tl-tool-btn" title="Tốc độ">⏱</button>
          <button className="tl-tool-btn" title="Hiệu ứng">✨</button>
          <div className="tl-right-tools">
            <button className="tl-tool-btn" title="Thu nhỏ">🔍−</button>
            <button className="tl-tool-btn" title="Phóng to">🔍＋</button>
          </div>
        </div>

        {/* Ruler */}
        <div className="timeline-ruler">
          <div className="ruler-marks">
            {['00:00', '00:05', '00:10', '00:15', '00:20', '00:25', '00:30', '00:35'].map((t, i) => (
              <span key={i} className="ruler-mark" style={{left: `${i * 14.28}%`}}>{t}</span>
            ))}
          </div>
          <div className="playhead" style={{left: '108px'}} />
        </div>

        {/* Tracks */}
        <div className="tracks-area">
          {/* Subtitle track */}
          <div className="track">
            <div className="track-label">
              🔤 Sub/Dub
              <div className="track-label-icons">
                <span className="track-label-icon">🔒</span>
                <span className="track-label-icon">👁</span>
              </div>
            </div>
            <div className="track-content">
              {videos.length > 0 && (
                <>
                  <div className="clip sub-clip s1">AI Thế nào mà</div>
                  <div className="clip sub-clip s2">AI Chính là</div>
                  <div className="clip sub-clip s3">AI Cỏ trúc xanh, cây sồng...</div>
                  <div className="clip sub-clip s4">AI Được á</div>
                  <div className="clip sub-clip s5">AI Khúc hiến lê cũ</div>
                </>
              )}
            </div>
          </div>

          {/* Video track */}
          <div className="track">
            <div className="track-label">
              🎬 Video
              <div className="track-label-icons">
                <span className="track-label-icon">🔒</span>
                <span className="track-label-icon">👁</span>
              </div>
            </div>
            <div className="track-content" style={{minHeight: '48px'}}>
              {videos.length > 0 && (
                <div className="clip video-clip">
                  <div className="clip-thumbnails">
                    {Array(12).fill(0).map((_, i) => (
                      <div key={i} className="clip-thumb" />
                    ))}
                  </div>
                  <span style={{zIndex: 1, position: 'relative'}}>
                    {videos[0]?.name || 'Video clip'}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Audio track */}
          <div className="track">
            <div className="track-label">
              🔊 Audio
              <div className="track-label-icons">
                <span className="track-label-icon">🔒</span>
                <span className="track-label-icon">👁</span>
              </div>
            </div>
            <div className="track-content">
              {videos.length > 0 && (
                <div className="clip audio-clip">Âm thanh gốc + Lồng tiếng</div>
              )}
            </div>
          </div>
        </div>
      </footer>
      
      {/* ======================= LOG VIEWER (TERMINAL) ======================= */}
      <div className={`log-viewer ${showLogs ? 'open' : ''}`} style={{
        position: 'fixed',
        bottom: 0,
        left: 0,
        right: 0,
        backgroundColor: '#1e1e1e',
        color: '#00ff00',
        fontFamily: 'monospace',
        transition: 'height 0.3s ease',
        height: showLogs ? '250px' : '30px',
        borderTop: '1px solid #333',
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column'
      }}>
        <div style={{
          padding: '5px 15px', 
          backgroundColor: '#2d2d2d', 
          display: 'flex', 
          justifyContent: 'space-between',
          cursor: 'pointer',
          userSelect: 'none'
        }} onClick={() => setShowLogs(!showLogs)}>
          <span style={{color: '#ccc', fontWeight: 'bold'}}>💻 Bot Terminal Logs {showLogs ? '▼' : '▲'}</span>
          <span style={{color: '#888', fontSize: '12px'}}>Click để {showLogs ? 'thu gọn' : 'mở rộng'}</span>
        </div>
        
        {showLogs && (
          <div style={{
            padding: '10px', 
            overflowY: 'auto', 
            flex: 1, 
            whiteSpace: 'pre-wrap',
            fontSize: '13px'
          }}>
            {botLogs || "Đang kết nối để lấy log từ Bot..."}
          </div>
        )}
      </div>
      {/* ==================================================================== */}

    </div>
  )
}

export default App

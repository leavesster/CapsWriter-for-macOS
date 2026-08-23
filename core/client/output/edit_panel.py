# coding: utf-8 -*-
"""
macOS 原生编辑框面板（PyObjC，2026-08-23）

编辑框模式：Caps 长按识别完成后，结果先进本面板；Enter=确认（先存标注、再恢复
按下 Caps 时的前台应用并粘贴上屏），Esc=取消上屏（按「有问题、未纠正」留存标注）。
面板打开期间 shortcut_manager 抑制新录音触发（v1 简化）。

线程模型：AppKit 面板必须在主线程创建与操作；本模块经 AppHelper.callAfter 把
显示/激活动作派发到主线程。确认/取消回调由 result_processor 提供，内部用
asyncio.run_coroutine_threadsafe 回客户端事件循环，因此主线程调用它们是安全的。
AppKit 导入失败（非 macOS/无 PyObjC）时所有入口安全降级。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

try:
    from AppKit import (
        NSPanel, NSTextField, NSTextFieldSquareBezel, NSFont,
        NSWindowStyleMaskTitled, NSWindowStyleMaskUtilityWindow,
        NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
        NSBackingStoreBuffered, NSFloatingWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
    )
    from Foundation import NSObject, NSRect, NSPoint, NSSize
    from PyObjCTools import AppHelper
    import objc
    # 本机 pyobjc 未导出 SwiftUI 时代的 NSWindowLevelFloating 常量名，
    # 使用等价经典常量 NSFloatingWindowLevel（值同为 3）
    NSWindowLevelFloating = NSFloatingWindowLevel
    _APPKIT_OK = True
except Exception:  # 非 macOS / PyObjC 缺失：整体功能静默关闭
    _APPKIT_OK = False

import logging
logger = logging.getLogger(__name__)

_PANEL_W, _PANEL_H = 680, 120
_controller = None          # EditorPanelController 单例（主线程创建）
_active = False             # 面板是否打开（主线程读写，读侧仅作提示用途）
_pending = False            # present_editor 已派发但 show_panel 尚未执行（跨线程窗口）
_pending_since = None       # _pending 置位时刻（time.monotonic()），用于看门狗判定派发丢失
_PENDING_TIMEOUT = 5.0      # 秒：超过视为 callAfter 派发丢失，自动复位


def is_available() -> bool:
    return _APPKIT_OK


def is_active() -> bool:
    # 同时覆盖 _pending：present_editor 在工作线程返回 True 后、主线程 show_panel
    # 置 _active 前存在一个竞态窗口，期间也应视为「面板打开」以抑制新录音。
    global _pending, _pending_since
    if _pending:
        # 看门狗：callAfter 派发若丢失（主线程 RunLoop 异常等），_pending 会永久
        # 卡 True 导致录音被永久静默抑制。超时后自动复位，宁可误弹一次面板
        # 的竞态窗口，也不让录音永久失效。
        if _pending_since is not None and \
                (time.monotonic() - _pending_since) > _PENDING_TIMEOUT:
            _pending = False
            _pending_since = None
            logger.warning("[edit-panel] present 派发疑似丢失，自动复位 pending 标志")
    return _active or _pending


def capture_frontmost_app() -> Optional[dict]:
    """在按下 Caps 的时刻调用：记录前台应用，作为稍后恢复焦点/上屏的目标。"""
    if not _APPKIT_OK:
        return None
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return {
            'pid': int(app.processIdentifier()),
            'bundle_id': app.bundleIdentifier(),
            'name': app.localizedName(),
        }
    except Exception as e:
        logger.debug(f"capture_frontmost_app 失败: {e}")
        return None


def init_panel() -> bool:
    """创建面板控制器。必须在主线程、NSApp 启动后调用一次（start_client_macos）。"""
    global _controller
    if not _APPKIT_OK:
        return False
    if _controller is not None:
        return True
    _controller = EditorPanelController.alloc().init()
    return _controller is not None


def present_editor(text: str, on_confirm: Callable[[str], None],
                   on_cancel: Callable[[], None]) -> bool:
    """线程安全入口：显示编辑框并预填识别文本。返回 False 时调用方回退直接上屏。"""
    global _pending, _pending_since
    if not _APPKIT_OK or _controller is None or _active or _pending:
        return False
    _pending = True
    _pending_since = time.monotonic()
    AppHelper.callAfter(_controller.show_panel, text, on_confirm, on_cancel)
    return True


def activate_app_sync(target: Optional[dict]) -> None:
    """把焦点交还给按下 Caps 时的前台应用。在客户端事件循环线程调用（阻塞 ~0.25s）。"""
    if not _APPKIT_OK or not target:
        return
    pid = int(target.get('pid', -1))
    if pid <= 0:
        return
    try:
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None or app.isTerminated():
            logger.info(f"[editor] 目标应用已退出（pid={pid}），跳过恢复焦点")
            return
        AppHelper.callAfter(
            app.activateWithOptions_, NSApplicationActivateIgnoringOtherApps)
        # 等 activate 生效再粘贴（粘贴靠目标应用处于前台接收 Cmd+V）
        time.sleep(0.25)
    except Exception as e:
        logger.warning(f"[editor] 恢复目标应用焦点失败: {e}")


if _APPKIT_OK:

    class EditorPanelController(NSObject):
        """NSPanel 持有者 + NSTextField 委托（Enter 确认 / Esc 取消）。"""

        def init(self):
            self = objc.super(EditorPanelController, self).init()
            frame = NSRect(NSPoint(0, 0), NSSize(_PANEL_W, _PANEL_H))
            # Utility 风格：小标题栏、置顶浮动、可加入所有空间（含全屏旁路），
            # 保证在任何应用前都能弹出（与输入法候选框同级别的交互模型）
            self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskTitled | NSWindowStyleMaskUtilityWindow
                | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered, False)
            self.panel.setTitle_('CapsWriter 识别结果')
            self.panel.setLevel_(NSWindowLevelFloating)
            self.panel.setHidesOnDeactivate_(False)
            self.panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary)
            self.panel.setReleasedWhenClosed_(False)
            self.panel.setDelegate_(self)

            self.field = NSTextField.textFieldWithString_('')
            self.field.setFont_(NSFont.systemFontOfSize_(16))
            self.field.setBezeled_(True)
            self.field.setBezelStyle_(NSTextFieldSquareBezel)
            self.field.setDelegate_(self)
            self.field.setFrame_(NSRect(NSPoint(16, 16), NSSize(_PANEL_W - 32, 44)))
            self.panel.contentView().addSubview_(self.field)

            self._on_confirm = None
            self._on_cancel = None
            return self

        # ---- 显示/关闭（全部主线程）----
        def show_panel(self, text, on_confirm, on_cancel):
            global _active, _pending, _pending_since
            _pending = False  # 派发已到达主线程，窗口关闭
            _pending_since = None
            if _active:
                return
            _active = True
            self._on_confirm = on_confirm
            self._on_cancel = on_cancel
            try:
                self.field.setStringValue_(text)
                # 屏幕上方 1/3 水平居中，固定出现位置（v1 不记忆位置）
                from AppKit import NSScreen
                screen = NSScreen.mainScreen().visibleFrame()
                x = screen.origin.x + (screen.size.width - _PANEL_W) / 2
                y = screen.origin.y + screen.size.height * 0.72
                self.panel.setFrameOrigin_(NSPoint(x, y))
                from AppKit import NSApplication, NSApp
                NSApp.activateIgnoringOtherApps_(True)
                self.panel.makeKeyAndOrderFront_(None)
                self.panel.makeFirstResponder_(self.field)
                self.field.selectText_(None)  # 全选：直接说话可整段替换，点击可局部改
            except Exception:
                # _active 已置位但面板未真正显示：回落清理，避免永久抑制录音。
                # 不调用任何回调：面板从未成功展示给用户，确认（上屏）与取消
                # （存「有问题、未纠正」标注）都基于「用户看过面板」这一前提，
                # 此时静默丢弃本次结果（用户重新口述）最安全。
                logger.error("[edit-panel] show_panel 异常，回落清理面板状态",
                             exc_info=True)
                self._on_confirm = None
                self._on_cancel = None
                self._dismiss()

        def _dismiss(self):
            global _active
            _active = False
            self.panel.orderOut_(None)

        def _confirm(self):
            final = str(self.field.stringValue())
            cb = self._on_confirm
            self._on_confirm = self._on_cancel = None
            self._dismiss()
            if cb is not None:
                # 退出竞态下 asyncio.run_coroutine_threadsafe 可能抛 RuntimeError
                # （事件循环已关闭），不能让它穿透 PyObjC 委托打断 AppKit 主线程
                try:
                    cb(final)
                except Exception:
                    logger.error("[edit-panel] 确认回调异常（忽略）", exc_info=True)

        def _cancel(self):
            cb = self._on_cancel
            self._on_confirm = self._on_cancel = None
            self._dismiss()
            if cb is not None:
                # 同上：取消回调异常不外抛，仅留痕
                try:
                    cb()
                except Exception:
                    logger.error("[edit-panel] 取消回调异常（忽略）", exc_info=True)

        # ---- NSTextField 委托：Enter/Esc ----
        # 方法名必须映射三段 selector control:textView:doCommandBySelector:
        def control_textView_doCommandBySelector_(self, control, tv, selector):
            if selector == 'insertNewline:':
                self._confirm()
                return True
            if selector in ('cancelOperation:', 'complete:'):
                self._cancel()
                return True
            return False

        # ---- NSWindow 委托：点红叉 = 取消 ----
        def windowWillClose_(self, notification):
            if _active:
                self._cancel()

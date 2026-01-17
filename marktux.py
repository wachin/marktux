import sys
import os
import re
from PyQt6.QtWidgets import (QApplication, QMainWindow, QPlainTextEdit, QTextEdit,
                            QVBoxLayout, QWidget, QTabWidget, QFileDialog,
                            QMessageBox, QMenuBar, QMenu, QStatusBar, QCheckBox)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRect, QSize
from PyQt6.QtGui import (QTextCharFormat, QColor, QFont, QSyntaxHighlighter, 
                         QAction, QKeySequence, QPainter, QTextFormat)
import pygments
from pygments.lexers import get_lexer_by_name, MarkdownLexer
from pygments.formatters import HtmlFormatter
from pygments import highlight
from pygments.styles import get_style_by_name
import markdown

class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)

class MarkdownTextEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)
        self.show_line_numbers = True
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.update_line_number_area_width(0)
        self.highlight_current_line()

    def line_number_area_width(self):
        if not self.show_line_numbers:
            return 0
            
        digits = 1
        max_num = max(1, self.blockCount())
        while max_num >= 10:
            max_num //= 10
            digits += 1
        
        space = 3 + self.fontMetrics().horizontalAdvance('9') * digits
        return space

    def update_line_number_area_width(self, _):
        if self.show_line_numbers:
            self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)
        else:
            self.setViewportMargins(0, 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), 
                                               self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        if not self.show_line_numbers:
            return
            
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#f0f0f0"))
        
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(QColor("#888"))
                painter.drawText(0, int(top), self.line_number_area.width() - 3, 
                                self.fontMetrics().height(),
                                Qt.AlignmentFlag.AlignRight, number)
            
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def highlight_current_line(self):
        if not self.show_line_numbers:
            return
            
        extra_selections = []
        
        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            line_color = QColor("#e6f3ff")
            selection.format.setBackground(line_color)
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)
        
        self.setExtraSelections(extra_selections)

    def keyPressEvent(self, event):
        # Implementar tabulación con 4 espacios al inicio de línea
        if event.key() == Qt.Key.Key_Tab:
            cursor = self.textCursor()
            if cursor.positionInBlock() == 0:  # Al inicio de la línea
                cursor.insertText("    ")  # 4 espacios
                return
                
        super().keyPressEvent(event)

    def toggle_line_numbers(self, visible):
        self.show_line_numbers = visible
        self.update_line_number_area_width(0)
        self.update()
        self.line_number_area.update()

class MarkdownHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.markdown_lexer = MarkdownLexer()
        self.code_blocks = {}  # Almacenar bloques de código y su lenguaje
        self.current_block_state = -1  # Estado del bloque actual

    def highlightBlock(self, text):
        # Verificar si estamos en un bloque de código
        prev_state = self.previousBlockState()

        # Si el bloque anterior estaba en un bloque de código, continuamos
        if prev_state == 1:
            # Buscar el final del bloque de código
            if text.strip() == "```":
                self.setCurrentBlockState(0)  # Fuera del bloque
                format = QTextCharFormat()
                format.setForeground(QColor("#032f62"))
                format.setBackground(QColor("#f6f8fa"))
                self.setFormat(0, len(text), format)
            else:
                self.setCurrentBlockState(1)  # Todavía en el bloque
                # Obtener el lenguaje del bloque de código
                block_number = self.currentBlock().blockNumber()
                if block_number - 1 in self.code_blocks:
                    language = self.code_blocks[block_number - 1]
                    self.highlightCodeBlock(text, language)
                else:
                    format = QTextCharFormat()
                    format.setForeground(QColor("#032f62"))
                    format.setBackground(QColor("#f6f8fa"))
                    self.setFormat(0, len(text), format)
            return

        # Si no estamos en un bloque de código, procesar como Markdown normal
        if text.strip().startswith("```"):
            # Inicio de un bloque de código
            self.setCurrentBlockState(1)  # Entramos en un bloque de código

            # Extraer el lenguaje si está especificado
            language = text.strip()[3:].strip() or "text"

            # Guardar el lenguaje para este bloque
            self.code_blocks[self.currentBlock().blockNumber()] = language

            # Resaltar la línea de inicio del bloque
            format = QTextCharFormat()
            format.setForeground(QColor("#032f62"))
            format.setBackground(QColor("#f6f8fa"))
            self.setFormat(0, len(text), format)
            return

        # Procesar como Markdown normal
        self.highlightMarkdown(text)

    def highlightMarkdown(self, text):
        # Usar Pygments para resaltar el Markdown
        tokens = self.markdown_lexer.get_tokens_unprocessed(text)

        for position, token_type, value in tokens:
            format = QTextCharFormat()

            # Obtener estilo del token
            style = get_style_by_name('default')  # Corregido aquí
            token_style = style.style_for_token(token_type)

            # Aplicar colores
            if token_style['color']:
                format.setForeground(QColor(f"#{token_style['color']}"))

            # Aplicar estilos de texto
            if token_style['bold']:
                format.setFontWeight(QFont.Weight.Bold)
            if token_style['italic']:
                format.setFontItalic(True)

            # Aplicar formato al texto
            self.setFormat(position, len(value), format)

        # Resaltado adicional para elementos que Pygments podría no manejar bien

        # Listas no ordenadas - ahora en rojo
        list_pattern = re.compile(r'^(\s*)([-*+])(\s+)')
        for match in list_pattern.finditer(text):
            # Formato para el marcador de lista
            list_format = QTextCharFormat()
            list_format.setForeground(QColor("#ff0000"))  # Rojo
            list_format.setFontWeight(QFont.Weight.Bold)
            self.setFormat(match.start(2), match.end(2) - match.start(2), list_format)

        # Listas ordenadas - ahora en rojo
        ordered_list_pattern = re.compile(r'^(\s*)(\d+\.)(\s+)')
        for match in ordered_list_pattern.finditer(text):
            # Formato para el número de lista
            list_format = QTextCharFormat()
            list_format.setForeground(QColor("#ff0000"))  # Rojo
            list_format.setFontWeight(QFont.Weight.Bold)
            self.setFormat(match.start(2), match.end(2) - match.start(2), list_format)

        # Tareas - con colores diferentes para completadas y pendientes
        task_pattern = re.compile(r'^(\s*)([-*+])\s+(\[)([xX ])(\])\s+')
        for match in task_pattern.finditer(text):
            # Formato para el marcador de tarea
            task_format = QTextCharFormat()
            task_format.setForeground(QColor("#ff0000"))  # Rojo
            task_format.setFontWeight(QFont.Weight.Bold)
            self.setFormat(match.start(2), match.end(2) - match.start(2), task_format)

            # Formato para el estado de la tarea
            state_format = QTextCharFormat()
            if match.group(4).strip() in ['x', 'X']:
                state_format.setForeground(QColor("#28a745"))  # Verde para completadas
                state_format.setFontWeight(QFont.Weight.Bold)
            else:
                state_format.setForeground(QColor("#6a737d"))  # Gris para pendientes
            self.setFormat(match.start(3), match.end(5) - match.start(3), state_format)

        # Encabezados
        header_pattern = re.compile(r'^(#+)\s+(.+)$')
        for match in header_pattern.finditer(text):
            # Formato para los #
            header_format = QTextCharFormat()
            header_format.setForeground(QColor("#032f62"))
            header_format.setFontWeight(QFont.Weight.Bold)
            self.setFormat(match.start(1), match.end(1) - match.start(1), header_format)

            # Formato para el texto del encabezado
            text_format = QTextCharFormat()
            text_format.setForeground(QColor("#032f62"))
            text_format.setFontWeight(QFont.Weight.Bold)
            self.setFormat(match.start(2), match.end(2) - match.start(2), text_format)

    def highlightCodeBlock(self, text, language):
        try:
            # Obtener el lexer para el lenguaje específico
            lexer = get_lexer_by_name(language)

            # Resaltar el código
            tokens = lexer.get_tokens_unprocessed(text)

            for position, token_type, value in tokens:
                format = QTextCharFormat()

                # Obtener estilo del token
                style = get_style_by_name('default')  # Corregido aquí
                token_style = style.style_for_token(token_type)

                # Aplicar colores
                if token_style['color']:
                    format.setForeground(QColor(f"#{token_style['color']}"))

                # Aplicar estilos de texto
                if token_style['bold']:
                    format.setFontWeight(QFont.Weight.Bold)
                if token_style['italic']:
                    format.setFontItalic(True)

                # Aplicar formato al texto
                self.setFormat(position, len(value), format)
        except:
            # Si hay un error, aplicar formato de código genérico
            format = QTextCharFormat()
            format.setForeground(QColor("#032f62"))
            format.setBackground(QColor("#f6f8fa"))
            self.setFormat(0, len(text), format)

class MarkdownTab(QWidget):
    def __init__(self, file_path=None):
        super().__init__()
        self.file_path = file_path
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        # Editor de texto personalizado
        self.editor = MarkdownTextEdit()

        # Configurar fuente
        font = QFont()
        font.setFamily("Fira Code")
        font.setPointSize(12)
        self.editor.setFont(font)

        # Resaltado de sintaxis
        self.highlighter = MarkdownHighlighter(self.editor.document())

        # Configurar actualización diferida
        self.timer = QTimer()
        self.timer.setSingleShot(True)  # Solo un disparo después de escribir
        self.timer.setInterval(500)    # Actualizar después de 500ms de inactividad
        self.timer.timeout.connect(self.updateHighlighting)
        self.editor.textChanged.connect(self.timer.start)

        layout.addWidget(self.editor)
        self.setLayout(layout)

        # Cargar archivo si se proporcionó
        if self.file_path:
            self.load_file(self.file_path)

    def updateHighlighting(self):
        # Forzar la actualización del resaltado
        self.highlighter.rehighlight()

    def load_file(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                self.editor.setPlainText(file.read())
            self.file_path = file_path
            return True
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo cargar el archivo: {str(e)}")
            return False

    def save_file(self, file_path=None):
        path = file_path or self.file_path
        if not path:
            return False

        try:
            with open(path, 'w', encoding='utf-8') as file:
                file.write(self.editor.toPlainText())
            self.file_path = path
            return True
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo guardar el archivo: {str(e)}")
            return False

    def get_file_name(self):
        if self.file_path:
            return os.path.basename(self.file_path)
        return "Nuevo documento"

class MarkdownEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("MarkTux - Editor Markdown")
        self.setGeometry(100, 100, 1200, 800)

        # Crear widget central con pestañas
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)

        self.layout = QVBoxLayout(self.central_widget)

        # Crear pestañas
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.layout.addWidget(self.tabs)

        # Crear barra de menú
        self.create_menu_bar()

        # Crear barra de estado
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Añadir una pestaña inicial
        self.add_new_tab()

        # Mostrar la ventana
        self.show()

    def create_menu_bar(self):
        menubar = self.menuBar()

        # Menú Archivo
        file_menu = menubar.addMenu("Archivo")

        new_action = QAction("Nuevo", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.add_new_tab)
        file_menu.addAction(new_action)

        open_action = QAction("Abrir", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        save_action = QAction("Guardar", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_file)
        file_menu.addAction(save_action)

        save_as_action = QAction("Guardar como", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self.save_file_as)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()

        exit_action = QAction("Salir", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Menú Editar
        edit_menu = menubar.addMenu("Editar")

        undo_action = QAction("Deshacer", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self.undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("Rehacer", self)
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.triggered.connect(self.redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        cut_action = QAction("Cortar", self)
        cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        cut_action.triggered.connect(self.cut)
        edit_menu.addAction(cut_action)

        copy_action = QAction("Copiar", self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self.copy)
        edit_menu.addAction(copy_action)

        paste_action = QAction("Pegar", self)
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        paste_action.triggered.connect(self.paste)
        edit_menu.addAction(paste_action)

        # Menú Ver
        view_menu = menubar.addMenu("Ver")
        
        toggle_line_numbers_action = QAction("Números de línea", self, checkable=True)
        toggle_line_numbers_action.setChecked(True)
        toggle_line_numbers_action.triggered.connect(self.toggle_line_numbers)
        view_menu.addAction(toggle_line_numbers_action)

        # Menú Pestaña
        tab_menu = menubar.addMenu("Pestaña")

        new_tab_action = QAction("Nueva pestaña", self)
        new_tab_action.triggered.connect(self.add_new_tab)
        tab_menu.addAction(new_tab_action)

        close_tab_action = QAction("Cerrar pestaña", self)
        close_tab_action.triggered.connect(self.close_current_tab)
        tab_menu.addAction(close_tab_action)

    def toggle_line_numbers(self, checked):
        tab = self.get_current_tab()
        if tab:
            tab.editor.toggle_line_numbers(checked)

    def add_new_tab(self, file_path=None):
        tab = MarkdownTab(file_path)
        index = self.tabs.addTab(tab, tab.get_file_name())
        self.tabs.setCurrentIndex(index)

        # Conectar señales para actualizar el título de la pestaña
        tab.editor.textChanged.connect(lambda: self.update_tab_title(index))

        return tab

    def update_tab_title(self, index):
        tab = self.tabs.widget(index)
        if tab:
            title = tab.get_file_name()
            if tab.editor.document().isModified():
                title += " *"
            self.tabs.setTabText(index, title)

    def close_tab(self, index):
        tab = self.tabs.widget(index)
        if tab:
            if tab.editor.document().isModified():
                reply = QMessageBox.question(
                    self, "Guardar cambios",
                    f"¿Desea guardar los cambios en {tab.get_file_name()}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
                )

                if reply == QMessageBox.StandardButton.Yes:
                    if not self.save_current_tab(tab):
                        return
                elif reply == QMessageBox.StandardButton.Cancel:
                    return

            self.tabs.removeTab(index)
            tab.deleteLater()

            # Si no hay pestañas, añadir una nueva
            if self.tabs.count() == 0:
                self.add_new_tab()

    def close_current_tab(self):
        current_index = self.tabs.currentIndex()
        if current_index >= 0:
            self.close_tab(current_index)

    def get_current_tab(self):
        current_index = self.tabs.currentIndex()
        if current_index >= 0:
            return self.tabs.widget(current_index)
        return None

    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Abrir archivo", "", "Archivos Markdown (*.md);;Todos los archivos (*)"
        )

        if file_path:
            # Verificar si el archivo ya está abierto
            for i in range(self.tabs.count()):
                tab = self.tabs.widget(i)
                if tab.file_path == file_path:
                    self.tabs.setCurrentIndex(i)
                    return

            # Si no está abierto, crear una nueva pestaña
            self.add_new_tab(file_path)

    def save_file(self):
        tab = self.get_current_tab()
        if tab:
            if tab.file_path:
                if tab.save_file():
                    self.status_bar.showMessage(f"Archivo guardado: {tab.file_path}", 3000)
                    tab.editor.document().setModified(False)
                    self.update_tab_title(self.tabs.currentIndex())
            else:
                self.save_file_as()

    def save_file_as(self):
        tab = self.get_current_tab()
        if tab:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Guardar archivo", "", "Archivos Markdown (*.md);;Todos los archivos (*)"
            )

            if file_path:
                if tab.save_file(file_path):
                    self.status_bar.showMessage(f"Archivo guardado: {file_path}", 3000)
                    tab.editor.document().setModified(False)
                    self.update_tab_title(self.tabs.currentIndex())

    def save_current_tab(self, tab):
        if tab.file_path:
            return tab.save_file()
        else:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Guardar archivo", "", "Archivos Markdown (*.md);;Todos los archivos (*)"
            )
            if file_path:
                return tab.save_file(file_path)
        return False

    def undo(self):
        tab = self.get_current_tab()
        if tab:
            tab.editor.undo()

    def redo(self):
        tab = self.get_current_tab()
        if tab:
            tab.editor.redo()

    def cut(self):
        tab = self.get_current_tab()
        if tab:
            tab.editor.cut()

    def copy(self):
        tab = self.get_current_tab()
        if tab:
            tab.editor.copy()

    def paste(self):
        tab = self.get_current_tab()
        if tab:
            tab.editor.paste()

    def closeEvent(self, event):
        # Verificar si hay pestañas con cambios sin guardar
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab.editor.document().isModified():
                reply = QMessageBox.question(
                    self, "Guardar cambios",
                    f"¿Desea guardar los cambios en {tab.get_file_name()}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
                )

                if reply == QMessageBox.StandardButton.Yes:
                    if not self.save_current_tab(tab):
                        event.ignore()
                        return
                elif reply == QMessageBox.StandardButton.Cancel:
                    event.ignore()
                    return

        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    editor = MarkdownEditor()
    sys.exit(app.exec())

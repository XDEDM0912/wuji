from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime
import os
import sqlite3
import json
import time
import requests
import re

# 初始化Flask应用
try:
    app = Flask(__name__)
    app.config['JSON_AS_ASCII'] = False  # 确保中文正常显示
    
    # 尝试导入OpenAI库，如果失败则使用requests
    OPENAI_AVAILABLE = False
    try:
        from openai import OpenAI
        OPENAI_AVAILABLE = True
        app.logger.info('成功导入OpenAI库')
    except ImportError as e:
        app.logger.warning(f'无法导入OpenAI库: {str(e)}，将使用requests库进行API调用')
        import requests
    except Exception as e:
        app.logger.error(f'OpenAI库初始化错误: {str(e)}')
except Exception as e:
    print(f'Flask应用初始化失败: {str(e)}')
    raise

# 创建数据库连接 - 添加重试逻辑防止数据库锁定
def get_db_connection():
    max_retries = 5
    retry_delay = 0.2
    
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect('chat_diary.db', timeout=10)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2  # 指数退避
            else:
                raise

# 初始化数据库
def init_db():
    conn = get_db_connection()
    # 创建表：用户聊天记录和会话上下文
    conn.execute('''
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT UNIQUE,
        date TEXT,
        context TEXT
    )
    ''')
    conn.execute('''
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        role TEXT,
        content TEXT,
        timestamp TEXT,
        FOREIGN KEY (session_id) REFERENCES chat_sessions (session_id)
    )
    ''')
    conn.commit()
    conn.close()



@app.route('/')
def index():
    # 生成最近的聊天记录列表，用于日历视图
    conn = get_db_connection()
    sessions = conn.execute('''
    SELECT DISTINCT date 
    FROM chat_sessions 
    ORDER BY date DESC
    ''').fetchall()
    conn.close()
    
    return render_template('index.html', sessions=[session['date'] for session in sessions])

@app.route('/reform')
def reform():
    return render_template('reform.html')

@app.route('/chat/<date>')
def chat(date):
    # 检查日期格式是否正确
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return redirect(url_for('index'))
    
    # 为这一天创建或获取会话ID
    session_id = f"chat_{date}"
    
    conn = get_db_connection()
    session = conn.execute('''
    SELECT * FROM chat_sessions WHERE session_id = ?
    ''', (session_id,)).fetchone()
    
    # 如果会话不存在，创建新会话
    if not session:
        conn.execute('''
        INSERT INTO chat_sessions (session_id, date, context)
        VALUES (?, ?, ?)
        ''', (session_id, date, json.dumps([{'role': 'system', 'content': '你是一个名为"聊天日记"的智能体助手，帮助用户记录日常并进行交流。'}])))
        conn.commit()
        context = [{'role': 'system', 'content': '你是一个名为"聊天日记"的智能体助手，帮助用户记录日常并进行交流。'}]
    else:
        context = json.loads(session['context'])
    
    # 获取这一天的所有聊天记录
    messages = conn.execute('''
    SELECT role, content FROM chat_messages 
    WHERE session_id = ? 
    ORDER BY timestamp ASC
    ''', (session_id,)).fetchall()
    conn.close()
    
    # 格式化聊天记录供前端显示
    chat_messages = []
    for msg in messages:
        chat_messages.append({
            'role': msg['role'],
            'content': msg['content']
        })
    
    return render_template('chat.html', date=date, messages=chat_messages)

@app.route('/send_message', methods=['POST'])
def send_message():
    try:
        # 检查请求是否包含JSON数据
        if not request.is_json:
            app.logger.error('请求不包含JSON数据: 内容类型应为application/json')
            app.logger.debug(f'请求头: {request.headers}')
            return jsonify({'error': '请求必须包含JSON数据'}), 400
            
        # 尝试解析JSON数据
        try:
            data = request.json
        except json.JSONDecodeError as e:
            app.logger.error(f'JSON解析错误: {str(e)}')
            return jsonify({'error': 'JSON格式错误'}), 400
        
        # 验证必要的字段
        if 'message' not in data or 'date' not in data:
            app.logger.error(f'缺少必要的字段: message={"message" in data}, date={"date" in data}')
            app.logger.debug(f'接收到的JSON数据: {data}')
            return jsonify({'error': '缺少必要的字段'}), 400
            
        user_message = data['message']
        date = data['date']
        
        # 验证日期格式
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            app.logger.error(f'日期格式错误: {date}，应为YYYY-MM-DD格式')
            return jsonify({'error': '日期格式错误'}), 400
            
        app.logger.info(f'接收到消息: 长度={len(user_message)}字符, 日期={date}')
        app.logger.debug(f'用户消息内容预览: {user_message[:100]}' + ('...' if len(user_message) > 100 else ''))
        
        session_id = f"chat_{date}"
        
        # 获取当前会话的上下文
        conn = None
        try:
            conn = get_db_connection()
            app.logger.debug(f'成功获取数据库连接，准备查询会话: {session_id}')
            
            session = conn.execute('''
            SELECT * FROM chat_sessions WHERE session_id = ?
            ''', (session_id,)).fetchone()
            
            if not session:
                app.logger.error(f'会话不存在: {session_id}')
                return jsonify({'error': '会话不存在'}), 400
            
            # 加载并验证上下文
            try:
                context = json.loads(session['context'])
                app.logger.debug(f'成功加载会话上下文，上下文长度: {len(context)}条消息')
            except json.JSONDecodeError as e:
                app.logger.error(f'上下文JSON解析错误: {str(e)}, 会话ID: {session_id}')
                return jsonify({'error': '会话上下文损坏'}), 500
            
            # 添加用户消息到上下文
            context.append({'role': 'user', 'content': user_message})
            app.logger.debug('用户消息已添加到上下文')
            
            # 保存用户消息到数据库
            timestamp = datetime.now().isoformat()
            conn.execute('''
            INSERT INTO chat_messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            ''', (session_id, 'user', user_message, timestamp))
            app.logger.debug('用户消息已保存到数据库')
            
            # 准备API请求数据
            # 转换上下文格式为API所需格式
            api_messages = []
            system_content = "你是一个名为'聊天日记'的智能体助手，帮助用户记录日常并进行交流。"
            
            for msg in context:
                if msg['role'] == 'system':
                    system_content = msg['content']  # 使用现有system消息
                else:
                    api_messages.append({
                        "role": msg['role'],
                        "content": msg['content']
                    })
            
            # 在消息列表前面添加system消息
            api_messages.insert(0, {
                "role": "system",
                "content": system_content
            })
            
            # 尝试使用OpenAI库调用API
            assistant_reply = None
            if OPENAI_AVAILABLE:
                assistant_reply = call_openai_api(api_messages, conn)
                
            # 如果OpenAI调用失败或未安装，则使用requests库
            if assistant_reply is None:
                assistant_reply = call_api_with_requests(api_messages, conn)
            
            # 验证回复内容
            if not assistant_reply or len(assistant_reply.strip()) == 0:
                app.logger.warning('AI返回空回复')
                assistant_reply = '抱歉，我暂时无法提供回复。'
            
            app.logger.debug(f'AI回复接收完成，长度: {len(assistant_reply)}字符')
            
            # 更新上下文
            context.append({'role': 'assistant', 'content': assistant_reply})
            app.logger.debug('AI回复已添加到上下文')
            
            # 保存智能体回复到数据库
            timestamp = datetime.now().isoformat()
            conn.execute('''
            INSERT INTO chat_messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            ''', (session_id, 'assistant', assistant_reply, timestamp))
            app.logger.debug('AI回复已保存到数据库')
            
            # 保存更新后的上下文
            try:
                conn.execute('''
                UPDATE chat_sessions SET context = ? WHERE session_id = ?
                ''', (json.dumps(context), session_id))
                app.logger.debug('会话上下文已更新')
            except Exception as e:
                app.logger.error(f'上下文更新失败: {str(e)}')
                conn.rollback()
                return jsonify({'error': '会话更新失败'}), 500
            
            conn.commit()
            app.logger.info(f'成功生成回复: {assistant_reply[:50]}' + ('...' if len(assistant_reply) > 50 else ''))
            return jsonify({'message': assistant_reply})
        except sqlite3.Error as e:
            app.logger.error(f'数据库操作错误: {str(e)}')
            if conn:
                conn.rollback()
            return jsonify({'error': '数据库操作失败'}), 500
        except Exception as e:
            app.logger.error(f'处理请求过程中发生未预期错误: {str(e)}', exc_info=True)
            if conn:
                conn.rollback()
            return jsonify({'error': f'服务处理错误: {str(e)}'}), 500
        finally:
            if conn:
                conn.close()
                app.logger.debug('数据库连接已关闭')
    except Exception as e:
        app.logger.critical(f'请求处理发生严重错误: {str(e)}', exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500


def call_openai_api(messages, conn):
    """使用OpenAI库调用API"""
    try:
        app.logger.info('开始使用OpenAI客户端调用API')
        client = OpenAI(
            base_url='https://api-inference.modelscope.cn/v1',
            api_key='ms-39ea0305-8f3f-4b24-b067-ae9526409792' # ModelScope Token
        )
        
        app.logger.debug(f'OpenAI请求模型: Qwen/Qwen3-Next-80B-A3B-Instruct')
        
        # 调用API获取回复（非流式）
        response = client.chat.completions.create(
            model='Qwen/Qwen3-Next-80B-A3B-Instruct', # ModelScope Model-Id
            messages=messages,
            max_tokens=1024
        )
        
        # 提取回复内容
        if hasattr(response, 'choices') and len(response.choices) > 0 and hasattr(response.choices[0], 'message'):
            return response.choices[0].message.content
        else:
            app.logger.error(f'OpenAI响应格式不符合预期: {response}')
            return None
    except Exception as e:
        app.logger.error(f'OpenAI API调用失败: {str(e)}')
        return None


def call_api_with_requests(messages, conn):
    """使用requests库直接调用API"""
    try:
        app.logger.info('开始使用requests库调用API')
        
        # 准备API请求数据
        api_url = 'https://api-inference.modelscope.cn/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ms-39ea0305-8f3f-4b24-b067-ae9526409792'
        }
        
        request_data = {
            "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
            "messages": messages,
            "max_tokens": 1024
        }
        
        app.logger.debug(f'API请求URL: {api_url}')
        app.logger.debug(f'API请求数据: {json.dumps(request_data, ensure_ascii=False)[:200]}...')
        
        # 发送请求
        response = requests.post(
            api_url,
            headers=headers,
            json=request_data,
            timeout=60  # 增加超时时间
        )
        
        app.logger.debug(f'API响应状态码: {response.status_code}')
        
        # 检查响应状态
        if response.status_code != 200:
            app.logger.error(f'API请求失败: 状态码={response.status_code}, 响应内容={response.text}')
            return None
        
        # 解析响应
        try:
            response_data = response.json()
        except json.JSONDecodeError as e:
            app.logger.error(f'API响应解析错误: {str(e)}, 响应内容={response.text}')
            return None
        
        # 提取回复内容
        if 'choices' in response_data and len(response_data['choices']) > 0 and 'message' in response_data['choices'][0]:
            return response_data['choices'][0]['message']['content']
        else:
            app.logger.error(f'API响应格式不符合预期: {response_data}')
            return None
    except requests.RequestException as e:
        app.logger.error(f'HTTP请求错误: {str(e)}')
        return None
    except Exception as e:
        app.logger.error(f'requests API调用过程中发生未预期错误: {str(e)}')
        return None

@app.route('/get_calendar_data', methods=['GET'])
def get_calendar_data():
    conn = get_db_connection()
    sessions = conn.execute('''
    SELECT date FROM chat_sessions
    ''').fetchall()
    conn.close()
    
    dates_with_chats = [session['date'] for session in sessions]
    return jsonify({'dates': dates_with_chats})

# 聊天接口 - 前端JavaScript调用的主接口
@app.route('/chat', methods=['POST'])
def chat_interface():
    try:
        # 解析请求数据
        if not request.is_json:
            app.logger.error('聊天请求不包含JSON数据')
            return jsonify({'error': '请求必须包含JSON数据'}), 400
        
        try:
            data = request.json
        except json.JSONDecodeError as e:
            app.logger.error(f'聊天请求JSON解析错误: {str(e)}')
            return jsonify({'error': 'JSON格式错误'}), 400
        
        # 验证必要的字段
        if 'message' not in data or 'session_id' not in data:
            app.logger.error('聊天请求缺少必要字段')
            return jsonify({'error': '缺少必要的字段'}), 400
        
        user_message = data['message']
        session_id = data['session_id']
        
        app.logger.info(f'接收到聊天消息: 长度={len(user_message)}字符, 会话ID={session_id}')
        
        # 获取当前会话的上下文
        conn = None
        try:
            conn = get_db_connection()
            
            # 检查会话是否存在，如果不存在则创建
            session = conn.execute('''
            SELECT * FROM chat_sessions WHERE session_id = ?
            ''', (session_id,)).fetchone()
            
            if not session:
                app.logger.info(f'会话不存在，创建新会话: {session_id}')
                # 为新会话设置系统消息
                
                # 根据会话ID判断使用哪种系统消息
                if session_id.startswith('reform_'):
                    # 改革开放主题专用系统消息
                    default_context = [{
                        'role': 'system', 
                        'content': '''你是一个改革开放历史进程专家，以数字人智能体的身份提供互动式教学服务。你的核心任务是：
                        1. 详细阐述改革开放的历史进程，包括重大事件、时间节点、关键决策和历史意义
                        2. 深入讲解中国特色社会主义经济理论的核心内容，如社会主义市场经济理论、初级阶段理论、基本经济制度等
                        3. 以专业、准确的语言回答关于改革开放的各种问题，数据要精确，分析要深入
                        4. 能够结合历史事实和理论知识，解释改革开放对中国发展的深远影响
                        5. 在回答中体现出历史的连贯性和理论的系统性
                        6. 保持学术性和教育性，但语言要通俗易懂，适合教学使用'''
                    }]
                else:
                    # 通用智能助手系统消息
                    default_context = [{
                        'role': 'system', 
                        'content': '你是一个智能助手，可以帮助用户回答问题，提供信息和支持。请以友好、专业的方式回答用户的问题。'
                    }]
                
                conn.execute('''
                INSERT INTO chat_sessions (session_id, date, context)
                VALUES (?, ?, ?)
                ''', (session_id, datetime.now().strftime('%Y-%m-%d'), json.dumps(default_context)))
                conn.commit()
                context = default_context
            else:
                # 加载现有上下文
                try:
                    context = json.loads(session['context'])
                except json.JSONDecodeError:
                    app.logger.warning('上下文解析错误，使用默认上下文')
                    if session_id.startswith('reform_'):
                        # 改革开放主题专用系统消息
                        context = [{
                            'role': 'system', 
                            'content': '''你是一个改革开放历史进程专家，以数字人智能体的身份提供互动式教学服务。你的核心任务是：
                            1. 详细阐述改革开放的历史进程，包括重大事件、时间节点、关键决策和历史意义
                            2. 深入讲解中国特色社会主义经济理论的核心内容，如社会主义市场经济理论、初级阶段理论、基本经济制度等
                            3. 以专业、准确的语言回答关于改革开放的各种问题，数据要精确，分析要深入
                            4. 能够结合历史事实和理论知识，解释改革开放对中国发展的深远影响
                            5. 在回答中体现出历史的连贯性和理论的系统性
                            6. 保持学术性和教育性，但语言要通俗易懂，适合教学使用'''
                        }]
                    else:
                        # 通用智能助手系统消息
                        context = [{
                            'role': 'system', 
                            'content': '你是一个智能助手，可以帮助用户回答问题，提供信息和支持。请以友好、专业的方式回答用户的问题。'
                        }]
            
            # 添加用户消息到上下文
            context.append({'role': 'user', 'content': user_message})
            
            # 保存用户消息到数据库
            timestamp = datetime.now().isoformat()
            conn.execute('''
            INSERT INTO chat_messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            ''', (session_id, 'user', user_message, timestamp))
            
            # 准备API请求数据
            api_messages = []
            system_content = "你是一个智能助手，可以帮助用户回答问题，提供信息和支持。请以友好、专业的方式回答用户的问题。"
            
            for msg in context:
                if msg['role'] == 'system':
                    system_content = msg['content']
                else:
                    api_messages.append(msg)
            
            # 在消息列表前面添加system消息
            api_messages.insert(0, {"role": "system", "content": system_content})
            
            # 调用API获取回复
            assistant_reply = None
            if OPENAI_AVAILABLE:
                assistant_reply = call_openai_api(api_messages, conn)
            
            if assistant_reply is None:
                assistant_reply = call_api_with_requests(api_messages, conn)
            
            # 处理API响应为空的情况
            if not assistant_reply or len(assistant_reply.strip()) == 0:
                assistant_reply = '抱歉，我暂时无法提供回复。'
            
            # 更新上下文
            context.append({'role': 'assistant', 'content': assistant_reply})
            
            # 保存智能体回复到数据库
            timestamp = datetime.now().isoformat()
            conn.execute('''
            INSERT INTO chat_messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            ''', (session_id, 'assistant', assistant_reply, timestamp))
            
            # 保存更新后的上下文
            conn.execute('''
            UPDATE chat_sessions SET context = ? WHERE session_id = ?
            ''', (json.dumps(context), session_id))
            
            conn.commit()
            app.logger.info(f'成功生成回复: {assistant_reply[:50]}' + ('...' if len(assistant_reply) > 50 else ''))
            return jsonify({'response': assistant_reply})
        except sqlite3.Error as e:
            app.logger.error(f'数据库操作错误: {str(e)}')
            if conn:
                conn.rollback()
            return jsonify({'error': '数据库操作失败'}), 500
        except Exception as e:
            app.logger.error(f'处理聊天请求过程中发生错误: {str(e)}')
            if conn:
                conn.rollback()
            return jsonify({'error': f'服务处理错误: {str(e)}'}), 500
        finally:
            if conn:
                conn.close()
    except Exception as e:
        app.logger.critical(f'聊天请求处理发生严重错误: {str(e)}')
        return jsonify({'error': '服务器内部错误'}), 500

# 清除历史记录接口
@app.route('/clear_history', methods=['POST'])
def clear_history():
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须包含JSON数据'}), 400
        
        data = request.json
        if 'session_id' not in data:
            return jsonify({'error': '缺少会话ID'}), 400
        
        session_id = data['session_id']
        app.logger.info(f'清除会话历史记录: {session_id}')
        
        conn = get_db_connection()
        # 删除该会话的所有消息
        conn.execute('''
        DELETE FROM chat_messages WHERE session_id = ?
        ''', (session_id,))
        
        # 重置上下文，只保留系统消息
        if session_id.startswith('reform_'):
            # 改革开放主题专用系统消息
            default_context = [{
                'role': 'system', 
                'content': '''你是一个改革开放历史进程专家，以数字人智能体的身份提供互动式教学服务。你的核心任务是：
                1. 详细阐述改革开放的历史进程，包括重大事件、时间节点、关键决策和历史意义
                2. 深入讲解中国特色社会主义经济理论的核心内容，如社会主义市场经济理论、初级阶段理论、基本经济制度等
                3. 以专业、准确的语言回答关于改革开放的各种问题，数据要精确，分析要深入
                4. 能够结合历史事实和理论知识，解释改革开放对中国发展的深远影响
                5. 在回答中体现出历史的连贯性和理论的系统性
                6. 保持学术性和教育性，但语言要通俗易懂，适合教学使用'''
            }]
        else:
            # 通用智能助手系统消息
            default_context = [{
                'role': 'system', 
                'content': '你是一个智能助手，可以帮助用户回答问题，提供信息和支持。请以友好、专业的方式回答用户的问题。'
            }]
        conn.execute('''
        UPDATE chat_sessions SET context = ? WHERE session_id = ?
        ''', (json.dumps(default_context), session_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f'清除历史记录失败: {str(e)}')
        return jsonify({'error': str(e)}), 500

# 获取历史记录接口
@app.route('/history', methods=['POST'])
def get_history():
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须包含JSON数据'}), 400
        
        data = request.json
        if 'session_id' not in data:
            return jsonify({'error': '缺少会话ID'}), 400
        
        session_id = data['session_id']
        
        conn = get_db_connection()
        # 查询该会话的所有消息
        messages = conn.execute('''
        SELECT role, content FROM chat_messages 
        WHERE session_id = ? 
        ORDER BY timestamp ASC
        ''', (session_id,)).fetchall()
        conn.close()
        
        # 格式化消息
        history = []
        for msg in messages:
            history.append({
                'role': msg['role'],
                'content': msg['content']
            })
        
        return jsonify({'history': history})
    except Exception as e:
        app.logger.error(f'获取历史记录失败: {str(e)}')
        return jsonify({'error': str(e)}), 500

# 处理多JSON对象响应的辅助函数
def parse_json_with_multiple_objects(text):
    """解析可能包含多个JSON对象的文本"""
    try:
        # 尝试直接解析
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            # 使用正则表达式尝试提取多个JSON对象
            json_objects = []
            # 匹配可能的JSON对象
            pattern = r'\{[^}]*\}'
            matches = re.findall(pattern, text)
            
            for match in matches:
                try:
                    json_obj = json.loads(match)
                    # 寻找包含choices字段的有效对象
                    if 'choices' in json_obj:
                        return json_obj
                    json_objects.append(json_obj)
                except:
                    continue
            
            # 如果找到多个对象，返回第一个
            if json_objects:
                return json_objects[0]
        except:
            pass
        
        # 都失败则返回错误信息
        app.logger.error(f'无法解析JSON响应: {text[:100]}...')
        return None

# 修改call_api_with_requests函数，使其使用我们的新解析函数
def call_api_with_requests(messages, conn):
    """使用requests库直接调用API，增加对多JSON对象响应的支持"""
    try:
        app.logger.info('开始使用requests库调用API')
        
        # 准备API请求数据
        api_url = 'https://api-inference.modelscope.cn/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ms-39ea0305-8f3f-4b24-b067-ae9526409792'
        }
        
        request_data = {
            "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
            "messages": messages,
            "max_tokens": 1024
        }
        
        app.logger.debug(f'API请求URL: {api_url}')
        app.logger.debug(f'API请求数据: {json.dumps(request_data, ensure_ascii=False)[:200]}...')
        
        # 发送请求
        response = requests.post(
            api_url,
            headers=headers,
            json=request_data,
            timeout=60  # 增加超时时间
        )
        
        app.logger.debug(f'API响应状态码: {response.status_code}')
        
        # 检查响应状态
        if response.status_code != 200:
            app.logger.error(f'API请求失败: 状态码={response.status_code}, 响应内容={response.text}')
            return None
        
        # 使用我们的解析函数解析响应
        response_data = parse_json_with_multiple_objects(response.text)
        
        if response_data is None:
            app.logger.error('API响应解析失败')
            return None
        
        # 提取回复内容
        if 'choices' in response_data and len(response_data['choices']) > 0 and 'message' in response_data['choices'][0]:
            return response_data['choices'][0]['message']['content']
        else:
            app.logger.error(f'API响应格式不符合预期: {response_data}')
            return None
    except requests.RequestException as e:
        app.logger.error(f'HTTP请求错误: {str(e)}')
        return None
    except Exception as e:
        app.logger.error(f'requests API调用过程中发生未预期错误: {str(e)}')
        return None

# 初始化数据库
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
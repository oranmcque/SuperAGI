from typing import List, Dict, Union
from sqlalchemy import func, distinct, and_
from sqlalchemy.orm import Session
from sqlalchemy import Integer
from fastapi import HTTPException
from superagi.models.events import Event
from superagi.models.tool import Tool
from superagi.models.toolkit import Toolkit
from sqlalchemy import or_
from sqlalchemy.sql import label
from datetime import datetime
from superagi.models.agent_config import AgentConfiguration
import pytz

class ToolsHandler:
    def __init__(self, session: Session, organisation_id: int):
        self.session = session
        self.organisation_id = organisation_id

    def get_tool_and_toolkit(self):
        tools_and_toolkits = self.session.query(
            Tool.name.label('tool_name'), Toolkit.name.label('toolkit_name')).join(
            Toolkit, Tool.toolkit_id == Toolkit.id).all()

        return {item.tool_name: item.toolkit_name for item in tools_and_toolkits}

    def calculate_tool_usage(self) -> List[Dict[str, int]]:
        tool_usage = []
        tool_used_subquery = self.session.query(
            Event.event_property['tool_name'].label('tool_name'),
            Event.agent_id
        ).filter_by(event_name="tool_used", org_id=self.organisation_id).subquery()

        agent_count = self.session.query(
            tool_used_subquery.c.tool_name,
            func.count(func.distinct(tool_used_subquery.c.agent_id)).label('unique_agents')
        ).group_by(tool_used_subquery.c.tool_name).subquery()

        total_usage = self.session.query(
            tool_used_subquery.c.tool_name,
            func.count(tool_used_subquery.c.tool_name).label('total_usage')
        ).group_by(tool_used_subquery.c.tool_name).subquery()

        query = self.session.query(
            agent_count.c.tool_name,
            agent_count.c.unique_agents,
            total_usage.c.total_usage,
        ).join(total_usage, total_usage.c.tool_name == agent_count.c.tool_name)

        tool_and_toolkit = self.get_tool_and_toolkit()

        result = query.all()

        tool_usage = [{
            'tool_name': row.tool_name,
            'unique_agents': row.unique_agents,
            'total_usage': row.total_usage,
            'toolkit': tool_and_toolkit.get(row.tool_name, None)
        } for row in result]

        return tool_usage
    
    def get_tool_usage_by_name(self, tool_name: str) -> Dict[str, Dict[str, int]]:
        is_tool_name_valid = self.session.query(Tool).filter_by(name=tool_name).first()

        if not is_tool_name_valid:
            raise HTTPException(status_code=404, detail="Tool not found")
        formatted_tool_name = tool_name.lower().replace(" ", "")

        tool_used_event = self.session.query(
            Event.event_property['tool_name'].label('tool_name'), 
            func.count(Event.id).label('tool_calls'),
            func.count(distinct(Event.agent_id)).label('tool_unique_agents')
        ).filter(
            Event.event_name == 'tool_used', 
            Event.org_id == self.organisation_id,
            Event.event_property['tool_name'].astext == formatted_tool_name
        ).group_by(
            Event.event_property['tool_name']
        ).first()

        if tool_used_event is None:
            return {}

        tool_data = {
                'tool_calls': tool_used_event.tool_calls,
                'tool_unique_agents': tool_used_event.tool_unique_agents
            }

        return tool_data
    
    def get_tool_events_by_name(self, tool_name: str) -> List[Dict[str, Union[str, int, List[str]]]]:
        is_tool_name_valid = self.session.query(Tool).filter_by(name=tool_name).first()

        if not is_tool_name_valid:
            raise HTTPException(status_code=404, detail="Tool not found")

        formatted_tool_name = tool_name.lower().replace(" ", "")

        event_run_created_ids = self.session.query(
            Event.id,
            Event.agent_id,
            label('agent_execution_id', 
                Event.event_property['agent_execution_id'].astext),
            Event.created_at
        ).filter(
            Event.org_id == self.organisation_id,
            Event.event_name == 'run_created'
        ).all()

        results = []

        for event in event_run_created_ids:
            min_id = event.id

            next_event = self.session.query(Event).filter(
                Event.org_id == self.organisation_id,
                Event.event_name == 'run_created', 
                Event.agent_id == event.agent_id, 
                Event.id > event.id
            ).order_by(Event.id).first()

            max_id = next_event.id if next_event else float('inf')

            event_run = self.session.query(
                Event.agent_id,
                label('tokens_consumed', func.sum(Event.event_property['tokens_consumed'].astext.cast(Integer))),
                label('calls', func.sum(Event.event_property['calls'].astext.cast(Integer))),
                label('name', func.max(Event.event_property['name'].astext))
            ).filter(
                Event.org_id == self.organisation_id,
                or_(Event.event_name == 'run_completed', Event.event_name == 'run_iteration_limit_crossed'),
                Event.agent_id == event.agent_id, 
                Event.id.between(min_id, max_id)
            ).group_by(Event.agent_id).first()

            matching_tool = self.session.query(
                Event.agent_id
            ).filter(
                Event.org_id == self.organisation_id,
                Event.event_name == 'tool_used',
                Event.event_property['tool_name'].astext == formatted_tool_name,
                Event.agent_id == event.agent_id, 
                Event.id.between(min_id, max_id)
            ).first()

            if event_run is None or matching_tool is None:
                continue

            other_tools = self.session.query(
                func.array_agg(distinct(Event.event_property['tool_name'].astext)).label('other_tools')
            ).filter(
                Event.org_id == self.organisation_id,
                Event.event_name == 'tool_used',
                Event.event_property['tool_name'].astext != formatted_tool_name,
                Event.agent_id == event.agent_id, 
                Event.id.between(min_id, max_id)
            ).first()

            event_agent_created = self.session.query(
                Event.agent_id,
                label('agent_name', 
                    func.max(Event.event_property['agent_name'].astext)),
                label('model', 
                    func.max(Event.event_property['model'].astext))
            ).filter(
                Event.org_id == self.organisation_id,
                Event.event_name == 'agent_created', 
                Event.agent_id == event.agent_id
            ).group_by(Event.agent_id).first()

            try:
                user_timezone = AgentConfiguration.get_agent_config_by_key_and_agent_id(session=self.session, key='user_timezone', agent_id=event.agent_id)
                if user_timezone and user_timezone.value != 'None':
                    tz = pytz.timezone(user_timezone.value)
                else:
                    tz = pytz.timezone('GMT')       
            except AttributeError:
                tz = pytz.timezone('GMT')

            
            actual_time = event.created_at.astimezone(tz).strftime("%d %B %Y %H:%M")
            result_dict = {
                'agent_id': event.agent_id,
                'created_at': actual_time,
                'agent_execution_id': event.agent_execution_id,
                'tokens_consumed': event_run.tokens_consumed,
                'calls': event_run.calls,
                'agent_execution_name': event_run.name,
                'other_tools': other_tools.other_tools if other_tools else None,
                'agent_name': event_agent_created.agent_name,
                'model': event_agent_created.model
            }

            results.append(result_dict)

        results = sorted(results, key=lambda x: datetime.strptime(x['created_at'], '%d %B %Y %H:%M'), reverse=True)

        return results
    
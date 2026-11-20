from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base
class ExclusionTrial(Base):
    __tablename__='exclusion_trials'
    __table_args__=(UniqueConstraint('study_id','basis_checksum'),)
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    name=Column(String(120),nullable=False)
    status=Column(String(20),default='queued')
    forced_keys=Column(JSON,nullable=False)
    basis=Column(JSON,nullable=False)
    basis_checksum=Column(String(64),nullable=False)
    frozen=Column(JSON,nullable=False)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
Trial=ExclusionTrial
